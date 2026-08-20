from collections.abc import Iterator

import httpx
import pytest

import conftest
from bearpond.server import repository

HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"


# ======================================================================================================================
@pytest.fixture
def api(live_server: str) -> Iterator[httpx.Client]:
    # exercises the real HTTP stack (starlette's httpx.Client+httpx combo is deprecated) against a fresh repo per test
    with httpx.Client(base_url=live_server, timeout=30.0) as http:
        yield http


# ----------------------------------------------------------------------------------------------------------------------
def begin_add(api: httpx.Client, rel_path: str, content: bytes) -> str:
    meta: dict = conftest.file_meta(rel_path, content).model_dump()
    response: httpx.Response = api.post("/transactions", json={"added": [meta], "removed": []})
    assert response.status_code == 200, response.text
    return response.json()["txn_id"]


# ----------------------------------------------------------------------------------------------------------------------
def commit_via_api(api: httpx.Client, rel_path: str, content: bytes) -> dict:
    txn_id: str = begin_add(api, rel_path, content)
    upload_response: httpx.Response = api.put(f"/transactions/{txn_id}/files/{rel_path}", content=content)
    assert upload_response.status_code == 200, upload_response.text
    commit_response: httpx.Response = api.post(f"/transactions/{txn_id}/commit")
    assert commit_response.status_code == 200, commit_response.text
    return commit_response.json()


# ----------------------------------------------------------------------------------------------------------------------
def test_manifest_is_404_on_fresh_lake(api: httpx.Client) -> None:
    assert api.get("/manifest").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_begin_with_empty_manifest_is_400(api: httpx.Client) -> None:
    response: httpx.Response = api.post("/transactions", json={"added": [], "removed": []})
    assert response.status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_unknown_transaction_is_404(api: httpx.Client) -> None:
    assert api.post("/transactions/no-such-txn/commit").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_with_wrong_content_is_400(api: httpx.Client) -> None:
    txn_id: str = begin_add(api, HIVE_PATH, CONTENT)
    response: httpx.Response = api.put(f"/transactions/{txn_id}/files/{HIVE_PATH}", content=b"not the declared content")
    assert response.status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_full_write_read_flow_over_http(api: httpx.Client) -> None:
    body: dict = commit_via_api(api, HIVE_PATH, CONTENT)
    assert body == {"seq": 1, "files_added_count": 1, "files_removed_count": 0}

    manifest: httpx.Response = api.get("/manifest")
    assert manifest.status_code == 200
    assert [f["path"] for f in manifest.json()["files"]] == [HIVE_PATH]

    download: httpx.Response = api.get(f"/files/{HIVE_PATH}")
    assert download.status_code == 200
    assert download.content == CONTENT


# ----------------------------------------------------------------------------------------------------------------------
def test_auth_is_open_when_no_token_configured(api: httpx.Client) -> None:
    # the repo fixture deletes BEARPOND_TOKEN, so every request passes unchecked
    assert api.get("/manifest").status_code == 404
    assert api.post("/transactions", json={"added": [], "removed": []}).status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_auth_rejects_missing_and_wrong_tokens(api: httpx.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEARPOND_TOKEN", "s3cret")

    assert api.get("/manifest").status_code == 401
    assert api.get("/manifest", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.post("/transactions", json={"added": [], "removed": []}).status_code == 401

    # the right token gets past auth (404 because the lake is still empty)
    assert api.get("/manifest", headers={"Authorization": "Bearer s3cret"}).status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_retry_returns_the_same_result(api: httpx.Client) -> None:
    # a client that lost the first commit response (crash, timeout) can safely retry — the answer comes from the
    # commit record, identical to the original
    txn_id: str = begin_add(api, HIVE_PATH, CONTENT)
    api.put(f"/transactions/{txn_id}/files/{HIVE_PATH}", content=CONTENT)

    first: httpx.Response = api.post(f"/transactions/{txn_id}/commit")
    retry: httpx.Response = api.post(f"/transactions/{txn_id}/commit")
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json() == first.json()


# ----------------------------------------------------------------------------------------------------------------------
def test_transaction_status_reports_open_committed_and_unknown(api: httpx.Client) -> None:
    txn_id: str = begin_add(api, HIVE_PATH, CONTENT)

    open_response: httpx.Response = api.get(f"/transactions/{txn_id}")
    assert open_response.status_code == 200
    assert open_response.json() == {"txn_id": txn_id, "status": "open", "seq": None}

    api.put(f"/transactions/{txn_id}/files/{HIVE_PATH}", content=CONTENT)
    api.post(f"/transactions/{txn_id}/commit")

    committed_response: httpx.Response = api.get(f"/transactions/{txn_id}")
    assert committed_response.status_code == 200
    assert committed_response.json() == {"txn_id": txn_id, "status": "committed", "seq": 1}

    assert api.get("/transactions/no-such-txn").status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_manifest_is_served_from_the_store_not_the_exports(api: httpx.Client, repo: repository.Repository) -> None:
    # the exported manifest files are an audit trail, not the source of truth — losing one changes nothing for readers
    commit_via_api(api, HIVE_PATH, CONTENT)
    (repo.manifest_root / "manifest-00000001.json").unlink()
    repo.latest_pointer.unlink()

    response: httpx.Response = api.get("/manifest")
    assert response.status_code == 200
    assert [f["path"] for f in response.json()["files"]] == [HIVE_PATH]
