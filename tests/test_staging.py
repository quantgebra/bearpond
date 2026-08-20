import argparse
import json
from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.client import bearpond_client
from bearpond.client import server_client
from bearpond.client import cli
from bearpond.server import metadata_store
from bearpond.server import repository

HIVE_PATH = "year=2024/month=01/part.parquet"
OTHER_PATH = "year=2024/month=02/other.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"other parquet bytes"


# ======================================================================================================================
@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # a working directory holding hive-layout parquet files, made the cwd — add/commit operate relative to it
    root: Path = tmp_path / "work"
    (root / "year=2024/month=01").mkdir(parents=True)
    (root / "year=2024/month=02").mkdir(parents=True)
    (root / HIVE_PATH).write_bytes(CONTENT)
    (root / OTHER_PATH).write_bytes(OTHER_CONTENT)
    monkeypatch.chdir(root)
    return root


# ----------------------------------------------------------------------------------------------------------------------
def test_stage_files_records_metadata(workdir: Path) -> None:
    staged: list[types.FileMetadata] = bearpond_client.BearpondClient(workdir).add([Path(HIVE_PATH)])
    assert [f.path for f in staged] == [HIVE_PATH]

    manifest: types.TransactionManifest = bearpond_client.BearpondClient(workdir).staged_manifest()
    assert [f.path for f in manifest.added] == [HIVE_PATH]
    assert manifest.added[0].sha256 == conftest.sha256_hex(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_stage_files_expands_directories(workdir: Path) -> None:
    (workdir / "year=2024/month=02/notes.txt").write_text("not parquet")

    staged: list[types.FileMetadata] = bearpond_client.BearpondClient(workdir).add([Path("year=2024")])
    assert [f.path for f in staged] == [HIVE_PATH, OTHER_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_stage_files_rejects_bad_targets(workdir: Path, tmp_path: Path) -> None:
    with pytest.raises(server_client.BearpondError, match="no such file"):
        bearpond_client.BearpondClient(workdir).add([Path("nope.parquet")])

    elsewhere: Path = tmp_path / "elsewhere.parquet"
    elsewhere.write_bytes(b"x")
    with pytest.raises(server_client.BearpondError, match="outside the working directory"):
        bearpond_client.BearpondClient(workdir).add([elsewhere])

    bad: Path = workdir / "year=2024/bad.csv"
    bad.write_bytes(b"x")
    with pytest.raises(server_client.BearpondError, match="not a parquet file"):
        bearpond_client.BearpondClient(workdir).add([Path("year=2024/bad.csv")])


# ----------------------------------------------------------------------------------------------------------------------
def test_cmd_commit_end_to_end(workdir: Path, live_server: str, repo: repository.Repository) -> None:
    cli.cmd_add(argparse.Namespace(paths=[Path("year=2024")]))
    cli.cmd_commit(argparse.Namespace(server=live_server, token=None, message="first commit"))

    # the staging area is cleared, the lake has the files, and the commit message is on the record
    assert not (workdir / bearpond_client.STAGED_NAME).exists()

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert sorted(f.path for f in manifest.files) == [HIVE_PATH, OTHER_PATH]

    records: list[metadata_store.CommitRecord] = repo.metadata_store.get_commit_record_list()
    assert records[0].reason == "first commit"


# ----------------------------------------------------------------------------------------------------------------------
def test_cmd_commit_refuses_drifted_files(workdir: Path, live_server: str, repo: repository.Repository) -> None:
    cli.cmd_add(argparse.Namespace(paths=[Path(HIVE_PATH)]))
    (workdir / HIVE_PATH).write_bytes(b"changed after staging")

    with pytest.raises(server_client.BearpondError, match="modified since staged"):
        cli.cmd_commit(argparse.Namespace(server=live_server, token=None, message=None))

    # the staging area survives so the user can re-stage and retry, and nothing reached the server
    assert (workdir / bearpond_client.STAGED_NAME).exists()
    assert conftest.current_manifest(repo) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_cmd_commit_with_nothing_staged(workdir: Path, live_server: str) -> None:
    with pytest.raises(server_client.BearpondError, match="nothing staged"):
        cli.cmd_commit(argparse.Namespace(server=live_server, token=None, message=None))


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_stages_removal_with_synced_metadata(workdir: Path) -> None:
    synced: dict = {HIVE_PATH: [len(CONTENT), conftest.sha256_hex(CONTENT)]}
    (workdir / bearpond_client.SYNC_STATE_NAME).write_text(json.dumps({"seq": 3, "files": synced}))

    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    manifest: types.TransactionManifest = workspace.rm([Path(HIVE_PATH)])

    assert manifest.added == []
    assert [f.path for f in manifest.removed] == [HIVE_PATH]
    assert manifest.removed[0].sha256 == conftest.sha256_hex(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_rejects_untracked_path(workdir: Path) -> None:
    with pytest.raises(server_client.BearpondError, match="not tracked"):
        bearpond_client.BearpondClient(workdir).rm([Path(HIVE_PATH)])


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_unstages_a_pending_add(workdir: Path) -> None:
    # a file staged for addition but never committed has nothing in the lake to remove — rm just unstage it
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    workspace.add([Path(HIVE_PATH)])
    manifest: types.TransactionManifest = workspace.rm([Path(HIVE_PATH)])
    assert manifest.added == []
    assert manifest.removed == []


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_commit_removes_from_lake_and_workspace(workdir: Path, live_server: str, repo: repository.Repository) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    with server_client.ServerClient(live_server) as server:
        workspace.add([Path("year=2024")])
        workspace.commit(server, message="add both")

        workspace.rm([Path(HIVE_PATH)])
        result: types.CommitResponse = workspace.commit(server, message="remove one")

    assert result.seq == 2

    # the lake, the synced state, and the working directory all dropped the path
    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert [f.path for f in manifest.files] == [OTHER_PATH]
    assert sorted(workspace.synced_files()) == [OTHER_PATH]
    assert workspace.synced_seq() == 2
    assert not (workdir / HIVE_PATH).exists()
    assert not (workdir / "year=2024/month=01").exists()
    assert (workdir / OTHER_PATH).read_bytes() == OTHER_CONTENT


# ----------------------------------------------------------------------------------------------------------------------
def test_status_reports_staged_untracked_modified_and_missing(workdir: Path) -> None:
    # synced state: PATH_A clean, OTHER_PATH tampered (hash differs), one path gone from disk entirely
    synced: dict = {
        HIVE_PATH: [len(CONTENT), conftest.sha256_hex(CONTENT)],
        OTHER_PATH: [len(OTHER_CONTENT), "0" * 64],
        "year=2024/month=03/gone.parquet": [1, "0" * 64],
    }
    (workdir / bearpond_client.SYNC_STATE_NAME).write_text(json.dumps({"seq": 7, "files": synced}))

    # one file staged, one simply sitting on disk unknown to the lake
    (workdir / "year=2024/month=04").mkdir(parents=True)
    (workdir / "year=2024/month=04/new.parquet").write_bytes(b"new")
    (workdir / "year=2025").mkdir(parents=True)
    (workdir / "year=2025/stray.parquet").write_bytes(b"stray")

    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    workspace.add([Path("year=2024/month=04/new.parquet")])

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert status.synced_seq == 7
    assert [f.path for f in status.staged_added] == ["year=2024/month=04/new.parquet"]
    assert status.untracked == ["year=2025/stray.parquet"]
    assert status.modified == [OTHER_PATH]
    assert status.missing == ["year=2024/month=03/gone.parquet"]
