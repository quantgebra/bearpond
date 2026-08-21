import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.client import bearpond_client
from bearpond.client import server_client
from bearpond.server import repository

PATH_A = "year=2024/month=01/a.parquet"
PATH_B = "year=2024/month=02/b.parquet"
CONTENT_A = b"alpha parquet bytes"
CONTENT_B = b"beta parquet bytes"


# ======================================================================================================================
@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    # a local tree mirroring the target hive layout, like the kind a workspace's add would stage
    root: Path = tmp_path / "source"
    (root / PATH_A).parent.mkdir(parents=True)
    (root / PATH_B).parent.mkdir(parents=True)
    (root / PATH_A).write_bytes(CONTENT_A)
    (root / PATH_B).write_bytes(CONTENT_B)
    return root


# ======================================================================================================================
@pytest.fixture
def client(live_server: str) -> Iterator[server_client.ServerClient]:
    with server_client.ServerClient(live_server) as active_client:
        yield active_client


# ----------------------------------------------------------------------------------------------------------------------
def commit_source_dir(client: server_client.ServerClient, source_dir: Path) -> types.CommitResponse:
    # drives the full transport-level write lifecycle: begin -> upload -> commit
    files: dict[str, Path] = {
        path.relative_to(source_dir).as_posix(): path for path in sorted(source_dir.rglob("*.parquet"))
    }
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_uuid: str = client.begin_transaction(declared)
    client.upload_files(txn_uuid, files)
    return client.commit(txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_on_empty_lake(client: server_client.ServerClient) -> None:
    manifest: types.Manifest = client.get_manifest()
    assert manifest.seq == 0
    assert manifest.files == []


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_flow_commits_and_is_visible_in_manifest(client: server_client.ServerClient, source_dir: Path) -> None:
    result: types.CommitResponse = commit_source_dir(client, source_dir)
    assert result.seq == 1
    assert result.files_added_count == 2

    manifest: types.Manifest = client.get_manifest()
    assert [f.path for f in manifest.files] == [PATH_A, PATH_B]


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_downloads_everything_and_records_state(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.sync(client)

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert (target / PATH_B).read_bytes() == CONTENT_B

    state: dict[str, bearpond_client.FileState] = workspace._tracked_files()
    assert sorted(state) == [PATH_A, PATH_B]
    assert state[PATH_A].sha256 == conftest.sha256_hex(CONTENT_A)
    assert workspace.tracked_manifest().seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_second_sync_refetches_nothing(
    client: server_client.ServerClient,
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.sync(client)

    downloads: list[str] = []
    original = client.download_file

    def counting_download(rel_path: str, dest_path: Path) -> str:
        downloads.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", counting_download)
    workspace.sync(client)
    assert downloads == []


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_prunes_removed_files_and_empty_dirs(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.sync(client)

    # a second transaction removes PATH_A from the lake, against the exact content the client observed
    removed_meta: types.FileMetadata = conftest.file_meta(PATH_A, CONTENT_A)
    txn_uuid: str = client.begin_transaction([], removed=[removed_meta])
    client.commit(txn_uuid)

    workspace.sync(client)

    assert not (target / PATH_A).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B
    # the emptied month=01 partition directory is pruned, while month=02's hierarchy stays
    assert not (target / "year=2024/month=01").exists()
    assert (target / "year=2024/month=02").is_dir()

    state: dict[str, bearpond_client.FileState] = workspace._tracked_files()
    assert sorted(state) == [PATH_B]
    assert workspace.tracked_manifest().seq == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_fails_on_checksum_mismatch(
    client: server_client.ServerClient,
    source_dir: Path,
    tmp_path: Path,
    repo: repository.Repository,
) -> None:
    commit_source_dir(client, source_dir)

    # the object on the server no longer matches what the manifest says it should be
    conftest.object_path(repo, CONTENT_A).write_bytes(b"corrupted after the fct")

    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(tmp_path / "mirror")
    with pytest.raises(server_client.BearpondError, match="checksum mismatch"):
        workspace.sync(client)


# ----------------------------------------------------------------------------------------------------------------------
def test_get_transaction_status(client: server_client.ServerClient, source_dir: Path) -> None:
    files: dict[str, Path] = {PATH_A: source_dir / PATH_A}
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_uuid: str = client.begin_transaction(declared)
    assert client.get_transaction_status(txn_uuid).status == "open"

    client.upload_files(txn_uuid, files)
    client.commit(txn_uuid)

    status: types.TransactionStatus = client.get_transaction_status(txn_uuid)
    assert status.status == "committed"
    assert status.seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_survives_deleted_exports(
    client: server_client.ServerClient,
    source_dir: Path,
    repo: repository.Repository,
) -> None:
    # the store is the source of truth — the exported manifest files can vanish without affecting readers
    commit_source_dir(client, source_dir)
    (repo.manifest_root / "manifest-00000001.json").unlink()
    repo.latest_pointer.unlink()

    manifest: types.Manifest = client.get_manifest()
    assert sorted(f.path for f in manifest.files) == [PATH_A, PATH_B]


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_stores_the_pristine_manifest(client: server_client.ServerClient, source_dir: Path, tmp_path: Path) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    bearpond_client.BearpondClient(target).sync(client)

    # the manifest file holds exactly what the server served — field-for-field, not a re-derivation
    raw: dict = json.loads((target / bearpond_client.MANIFEST_NAME).read_text())
    assert raw == client.get_manifest().model_dump()
    assert not (target / bearpond_client.PENDING_NAME).exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_stores_the_pristine_manifest(client: server_client.ServerClient, source_dir: Path) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A, source_dir / PATH_B])
    result: types.CommitResponse = workspace.commit(client, message="initial")

    raw: dict = json.loads((source_dir / bearpond_client.MANIFEST_NAME).read_text())
    assert raw["seq"] == result.seq == 1
    assert not (source_dir / bearpond_client.PENDING_NAME).exists()
    assert workspace.tracked_manifest().seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_resumes_from_pending_after_interruption(
    client: server_client.ServerClient,
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)

    # a sync that dies after the first download: the completed file is recorded in the pending overlay
    original = client.download_file
    calls: list[str] = []

    def fail_after_first(rel_path: str, dest_path: Path) -> str:
        if calls:
            raise server_client.BearpondError("boom")
        calls.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", fail_after_first)
    with pytest.raises(server_client.BearpondError, match="boom"):
        workspace.sync(client)

    # no manifest yet; the pending file marks the interrupted sync and records the completed download
    assert not (target / bearpond_client.MANIFEST_NAME).exists()
    pending: dict = json.loads((target / bearpond_client.PENDING_NAME).read_text())
    assert list(pending) == [PATH_A]

    # the retry downloads only what the overlay doesn't already cover, then adopts the full manifest
    def recording(rel_path: str, dest_path: Path) -> str:
        calls.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", recording)
    calls.clear()
    workspace.sync(client)

    assert calls == [PATH_B]
    final: dict = json.loads((target / bearpond_client.MANIFEST_NAME).read_text())
    assert final["seq"] == 1
    assert not (target / bearpond_client.PENDING_NAME).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B
