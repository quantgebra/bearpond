import importlib.metadata
from collections.abc import Iterator

import httpx
import pytest

import conftest
from bearpond.protocol import types
from bearpond.server import repository

HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"updated parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(live_server: str) -> Iterator[httpx.Client]:
    # exercises the real HTTP stack (starlette's httpx.Client+httpx combo is deprecated) against a fresh repo per test
    with httpx.Client(base_url=live_server, timeout=30.0) as http:
        yield http


# ----------------------------------------------------------------------------------------------------------------------
def begin_add(api: httpx.Client, rel_path: str, content: bytes) -> types.TxnUuid:
    meta: dict = conftest.file_meta(rel_path, content).model_dump()
    response: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions", json={"added": [meta], "removed": []})
    assert response.status_code == 200, response.text
    return response.json()["txn_uuid"]


# ----------------------------------------------------------------------------------------------------------------------
def commit_via_api(api: httpx.Client, rel_path: str, content: bytes) -> dict:
    txn_uuid: types.TxnUuid = begin_add(api, rel_path, content)
    upload_response: httpx.Response = api.put(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/files/{rel_path}", content=content)
    assert upload_response.status_code == 200, upload_response.text
    commit_response: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")
    assert commit_response.status_code == 200, commit_response.text
    return commit_response.json()


# ----------------------------------------------------------------------------------------------------------------------
def test_manifest_is_404_on_fresh_lake(api: httpx.Client) -> None:
    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_with_empty_manifest_is_400(api: httpx.Client) -> None:
    response: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions", json={"added": [], "removed": []})
    assert response.status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_and_abort_unknown_transaction_are_404(api: httpx.Client) -> None:
    base: str = f"/repos/{conftest.REPO_NAME}/transactions/no-such-txn"
    assert api.put(f"{base}/files/{HIVE_PATH}", content=CONTENT).status_code == 404
    assert api.delete(base).status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_unknown_transaction_is_404(api: httpx.Client) -> None:
    assert api.post(f"/repos/{conftest.REPO_NAME}/transactions/no-such-txn/commit").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_with_wrong_content_is_400(api: httpx.Client) -> None:
    txn_uuid: types.TxnUuid = begin_add(api, HIVE_PATH, CONTENT)
    response: httpx.Response = api.put(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/files/{HIVE_PATH}", content=b"not the declared content")
    assert response.status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_full_write_read_flow_over_http(api: httpx.Client) -> None:
    body: dict = commit_via_api(api, HIVE_PATH, CONTENT)
    assert body == {"seq": 1, "files_added_count": 1, "files_removed_count": 0, "files_updated_count": 0}

    manifest: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/manifest")
    assert manifest.status_code == 200
    assert [f["path"] for f in manifest.json()["files"]] == [HIVE_PATH]

    download: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/files/{HIVE_PATH}")
    assert download.status_code == 200
    assert download.content == CONTENT


# ----------------------------------------------------------------------------------------------------------------------
def test_auth_is_open_when_no_token_configured(api: httpx.Client) -> None:
    # the repo fixture deletes BEARPOND_TOKEN, so every request passes unchecked
    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest").status_code == 404
    assert api.post(f"/repos/{conftest.REPO_NAME}/transactions", json={"added": [], "removed": []}).status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_auth_rejects_missing_and_wrong_tokens(api: httpx.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEARPOND_TOKEN", "s3cret")

    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest").status_code == 401
    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.post(f"/repos/{conftest.REPO_NAME}/transactions", json={"added": [], "removed": []}).status_code == 401

    # the right token gets past auth (404 because the lake is still empty)
    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest", headers={"Authorization": "Bearer s3cret"}).status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_retry_returns_the_same_result(api: httpx.Client) -> None:
    # a client that lost the first commit response (crash, timeout) can safely retry — the answer comes from the
    # commit record, identical to the original
    txn_uuid: types.TxnUuid = begin_add(api, HIVE_PATH, CONTENT)
    api.put(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/files/{HIVE_PATH}", content=CONTENT)

    first: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")
    retry: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json() == first.json()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_records_the_user_and_reason(api: httpx.Client, repo: repository.Repository) -> None:
    # user and reason are declared at begin time — they're the transaction's intent, not commit-call parameters
    meta: dict = conftest.file_meta(HIVE_PATH, CONTENT).model_dump()
    begin_response: httpx.Response = api.post(
        f"/repos/{conftest.REPO_NAME}/transactions",
        json={"added": [meta], "user": "nightly-etl", "reason": "why we did it"},
    )
    assert begin_response.status_code == 200, begin_response.text
    txn_uuid: types.TxnUuid = begin_response.json()["txn_uuid"]
    api.put(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/files/{HIVE_PATH}", content=CONTENT)

    response: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")
    assert response.status_code == 200

    record: types.CommitRecord | None = repo.metadata_store.get_commit_record_by_txn_uuid(txn_uuid)
    assert record is not None
    assert record.user == "nightly-etl"
    assert record.reason == "why we did it"


# ----------------------------------------------------------------------------------------------------------------------
def test_transaction_status_reports_open_committed_and_unknown(api: httpx.Client) -> None:
    txn_uuid: types.TxnUuid = begin_add(api, HIVE_PATH, CONTENT)

    open_response: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}")
    assert open_response.status_code == 200
    open_body: dict = open_response.json()
    assert open_body["txn_uuid"] == txn_uuid
    assert open_body["status"] == "open"
    assert open_body["seq"] is None
    assert open_body["created_at"] is not None

    api.put(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/files/{HIVE_PATH}", content=CONTENT)
    api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")

    committed_response: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}")
    assert committed_response.status_code == 200
    committed_body: dict = committed_response.json()
    assert committed_body["txn_uuid"] == txn_uuid
    assert committed_body["status"] == "committed"
    assert committed_body["seq"] == 1
    assert committed_body["created_at"] == open_body["created_at"]

    assert api.get(f"/repos/{conftest.REPO_NAME}/transactions/no-such-txn").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_repository_create_and_list(api: httpx.Client) -> None:
    create: httpx.Response = api.post("/repos", json={"name": "second-repo"})
    assert create.status_code == 201
    assert create.json() == {"name": "second-repo"}

    listing: httpx.Response = api.get("/repos")
    assert sorted(r["name"] for r in listing.json()) == ["second-repo", conftest.REPO_NAME]

    # the new repo is a real, empty lake — its manifest 404s until a first commit
    assert api.get("/repos/second-repo/manifest").status_code == 404

    # a duplicate and a traversal-y name are client errors, not server faults
    assert api.post("/repos", json={"name": "second-repo"}).status_code == 409
    assert api.post("/repos", json={"name": "../escape"}).status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_repositories_are_isolated(api: httpx.Client) -> None:
    api.post("/repos", json={"name": "other"})
    commit_via_api(api, HIVE_PATH, CONTENT)

    # the commit landed in the default repo only
    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest").status_code == 200
    assert api.get("/repos/other/manifest").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_history_endpoint(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)
    commit_via_api(api, "year=2024/month=03/extra.parquet", b"extra")

    response: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/commits", params={"limit": 1})
    assert response.status_code == 200
    page: dict = response.json()
    assert [r["seq"] for r in page["records"]] == [2]
    assert page["next_cursor"] == 2

    rest: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/commits", params={"before_seq": page["next_cursor"]})
    assert [r["seq"] for r in rest.json()["records"]] == [1]
    assert rest.json()["next_cursor"] is None


# ----------------------------------------------------------------------------------------------------------------------
def test_directory_listing_over_http(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)

    # a prefix whose children are all deeper comes back as directory names relative to the requested path
    year_response: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/files/year=2024/")
    assert year_response.status_code == 200
    assert year_response.json()["files"] == []
    assert year_response.json()["directories"] == ["month=01"]

    month_response: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/files/year=2024/month=01/")
    assert month_response.status_code == 200
    assert month_response.json()["files"] == [{"name": "part.parquet", "size": len(CONTENT)}]
    assert month_response.json()["next_cursor"] is None

    assert api.get(f"/repos/{conftest.REPO_NAME}/files/year=1999/").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_manifest_by_seq_returns_that_version(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)
    commit_via_api(api, "year=2024/month=03/extra.parquet", b"extra")

    first: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/manifest", params={"seq": 1})
    assert first.status_code == 200
    assert [f["path"] for f in first.json()["files"]] == [HIVE_PATH]

    second: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/manifest", params={"seq": 2})
    assert second.status_code == 200
    assert sorted(f["path"] for f in second.json()["files"]) == [HIVE_PATH, "year=2024/month=03/extra.parquet"]


# ----------------------------------------------------------------------------------------------------------------------
def test_manifest_by_unknown_seq_is_404(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)
    assert api.get(f"/repos/{conftest.REPO_NAME}/manifest", params={"seq": 99}).status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_update_over_http(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)

    updated_meta: dict = {
        "path": HIVE_PATH,
        "old_sha256": conftest.sha256_hex(CONTENT),
        "old_size": len(CONTENT),
        "new_sha256": conftest.sha256_hex(OTHER_CONTENT),
        "new_size": len(OTHER_CONTENT),
    }
    begin: httpx.Response = api.post(
        f"/repos/{conftest.REPO_NAME}/transactions", json={"added": [], "removed": [], "updated": [updated_meta]}
    )
    assert begin.status_code == 200, begin.text
    txn_uuid: types.TxnUuid = begin.json()["txn_uuid"]
    assert begin.json()["updated_files"] == [HIVE_PATH]

    upload: httpx.Response = api.put(
        f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/files/{HIVE_PATH}", content=OTHER_CONTENT
    )
    assert upload.status_code == 200, upload.text

    commit: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"seq": 2, "files_added_count": 0, "files_removed_count": 0, "files_updated_count": 1}

    manifest: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/manifest")
    assert manifest.status_code == 200
    files: list[dict] = manifest.json()["files"]
    assert len(files) == 1
    assert files[0]["path"] == HIVE_PATH
    assert files[0]["sha256"] == conftest.sha256_hex(OTHER_CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_over_http(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)  # seq 1
    remove: httpx.Response = api.post(
        f"/repos/{conftest.REPO_NAME}/transactions",
        json={"added": [], "removed": [conftest.file_meta(HIVE_PATH, CONTENT).model_dump()]},
    )
    assert remove.status_code == 200, remove.text
    txn_uuid: types.TxnUuid = remove.json()["txn_uuid"]
    commit: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/{txn_uuid}/commit")
    assert commit.status_code == 200, commit.text  # seq 2

    response: httpx.Response = api.post(
        f"/repos/{conftest.REPO_NAME}/transactions/revert", json={"target_seq": 1, "reason": "bring it back"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"seq": 3, "files_added_count": 1, "files_removed_count": 0, "files_updated_count": 0}

    manifest: httpx.Response = api.get(f"/repos/{conftest.REPO_NAME}/manifest")
    assert [f["path"] for f in manifest.json()["files"]] == [HIVE_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_revert_to_invalid_seq_is_400(api: httpx.Client) -> None:
    commit_via_api(api, HIVE_PATH, CONTENT)
    response: httpx.Response = api.post(f"/repos/{conftest.REPO_NAME}/transactions/revert", json={"target_seq": 99})
    assert response.status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_version_endpoint(api: httpx.Client) -> None:
    response: httpx.Response = api.get("/version")
    assert response.status_code == 200, response.text
    body: dict = response.json()
    assert body["protocol_version"] == types.PROTOCOL_VERSION
    assert body["server_version"] == importlib.metadata.version("bearpond-server")
