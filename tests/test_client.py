from collections.abc import Iterator
from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.client import bearpond_client
from bearpond.server import repository

PATH_A = "year=2024/month=01/a.parquet"
PATH_B = "year=2024/month=02/b.parquet"
CONTENT_A = b"alpha parquet bytes"
CONTENT_B = b"beta parquet bytes"


# ======================================================================================================================
@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    # a local tree mirroring the target hive layout, like the kind cli.cmd_upload ingests
    root: Path = tmp_path / "source"
    (root / PATH_A).parent.mkdir(parents=True)
    (root / PATH_B).parent.mkdir(parents=True)
    (root / PATH_A).write_bytes(CONTENT_A)
    (root / PATH_B).write_bytes(CONTENT_B)
    return root


# ======================================================================================================================
@pytest.fixture
def client(live_server: str) -> Iterator[bearpond_client.BearpondClient]:
    with bearpond_client.BearpondClient(live_server) as active_client:
        yield active_client


# ----------------------------------------------------------------------------------------------------------------------
def commit_source_dir(client: bearpond_client.BearpondClient, source_dir: Path) -> types.CommitResponse:
    # drives the full client-side write lifecycle — the same begin -> upload -> commit sequence cli.cmd_upload uses
    files: dict[str, Path] = {
        path.relative_to(source_dir).as_posix(): path for path in sorted(source_dir.rglob("*.parquet"))
    }
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_id: str = client.begin_transaction(declared)
    client.upload_files(txn_id, files)
    return client.commit(txn_id)


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_on_empty_lake(client: bearpond_client.BearpondClient) -> None:
    manifest: types.Manifest = client.get_manifest()
    assert manifest.seq == 0
    assert manifest.files == []


# ----------------------------------------------------------------------------------------------------------------------
def test_upload_flow_commits_and_is_visible_in_manifest(client: bearpond_client.BearpondClient, source_dir: Path) -> None:
    result: types.CommitResponse = commit_source_dir(client, source_dir)
    assert result.seq == 1
    assert result.files_added_count == 2

    manifest: types.Manifest = client.get_manifest()
    assert [f.path for f in manifest.files] == [PATH_A, PATH_B]


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_downloads_everything_and_records_state(client: bearpond_client.BearpondClient, source_dir: Path, tmp_path: Path) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    client.sync(target)

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert (target / PATH_B).read_bytes() == CONTENT_B
    state: dict[str, bearpond_client.SyncedFileState] = bearpond_client.load_local_state(target)
    assert sorted(state) == [PATH_A, PATH_B]
    assert state[PATH_A].sha256 == conftest.sha256_hex(CONTENT_A)


# ----------------------------------------------------------------------------------------------------------------------
def test_second_sync_refetches_nothing(
    client: bearpond_client.BearpondClient,
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    client.sync(target)

    downloads: list[str] = []
    original = client.download_file

    def counting_download(rel_path: str, dest_path: Path) -> str:
        downloads.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", counting_download)
    client.sync(target)
    assert downloads == []


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_prunes_removed_files_and_empty_dirs(client: bearpond_client.BearpondClient, source_dir: Path, tmp_path: Path) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    client.sync(target)

    # a second transaction removes PATH_A from the lake, against the exact content the client observed
    removed_meta: types.FileMetadata = conftest.file_meta(PATH_A, CONTENT_A)
    txn_id: str = client.begin_transaction([], removed=[removed_meta])
    client.commit(txn_id)

    client.sync(target)

    assert not (target / PATH_A).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B
    # the emptied month=01 partition directory is pruned, while month=02's hierarchy stays
    assert not (target / "year=2024/month=01").exists()
    assert (target / "year=2024/month=02").is_dir()

    state: dict[str, bearpond_client.SyncedFileState] = bearpond_client.load_local_state(target)
    assert sorted(state) == [PATH_B]


# ----------------------------------------------------------------------------------------------------------------------
def test_sync_fails_on_checksum_mismatch(
    client: bearpond_client.BearpondClient,
    source_dir: Path,
    tmp_path: Path,
    repo: repository.Repository,
) -> None:
    commit_source_dir(client, source_dir)

    # the object on the server no longer matches what the manifest says it should be
    conftest.object_path(repo, CONTENT_A).write_bytes(b"corrupted after the fct")

    with pytest.raises(bearpond_client.BearpondError, match="checksum mismatch"):
        client.sync(tmp_path / "mirror")


# ----------------------------------------------------------------------------------------------------------------------
def test_get_transaction_status(client: bearpond_client.BearpondClient, source_dir: Path) -> None:
    files: dict[str, Path] = {PATH_A: source_dir / PATH_A}
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_id: str = client.begin_transaction(declared)
    assert client.get_transaction_status(txn_id).status == "open"

    client.upload_files(txn_id, files)
    client.commit(txn_id)

    status: types.TransactionStatus = client.get_transaction_status(txn_id)
    assert status.status == "committed"
    assert status.seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_survives_deleted_exports(
    client: bearpond_client.BearpondClient,
    source_dir: Path,
    repo: repository.Repository,
) -> None:
    # the store is the source of truth — the exported manifest files can vanish without affecting readers
    commit_source_dir(client, source_dir)
    (repo.manifest_root / "manifest-00000001.json").unlink()
    repo.latest_pointer.unlink()

    manifest: types.Manifest = client.get_manifest()
    assert sorted(f.path for f in manifest.files) == [PATH_A, PATH_B]
