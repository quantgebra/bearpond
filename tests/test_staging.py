import argparse
import json
from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.client import bearpond_client
from bearpond.client import server_client
from bearpond.client import cli
from bearpond.server import repository

HIVE_PATH = "year=2024/month=01/part.parquet"
OTHER_PATH = "year=2024/month=02/other.parquet"
CONTENT = b"fake parquet bytes"
OTHER_CONTENT = b"other parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def write_workspace_manifest_file(workdir: Path, manifest: dict) -> None:
    # a .bearpond/manifest.json holding the given manifest dump
    state_dir: Path = workdir / bearpond_client.STATE_DIR_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / bearpond_client.MANIFEST_NAME).write_text(json.dumps(manifest))


# ----------------------------------------------------------------------------------------------------------------------
def write_workspace_manifest(workdir: Path, seq: int, files: dict[str, list]) -> None:
    # a .bearpond/manifest.json in the current format: the verbatim manifest dump
    manifest: dict = {
        "seq": seq,
        "created_at": None,
        "files": [{"path": p, "sha256": v[0], "size": v[1]} for p, v in files.items()],
    }
    write_workspace_manifest_file(workdir, manifest)


# ----------------------------------------------------------------------------------------------------------------------
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

    # the status report shows the staged addition — the public view of the staging area
    status: bearpond_client.WorkspaceStatus = bearpond_client.BearpondClient(workdir).status()
    assert [f.path for f in status.staged_added] == [HIVE_PATH]
    assert status.staged_added[0].sha256 == conftest.sha256_hex(CONTENT)


# ----------------------------------------------------------------------------------------------------------------------
def test_stage_files_expands_directories(workdir: Path) -> None:
    (workdir / "year=2024/month=02/notes.txt").write_text("a non-parquet file")

    staged: list[types.FileMetadata] = bearpond_client.BearpondClient(workdir).add([Path("year=2024")])
    assert [f.path for f in staged] == [HIVE_PATH, "year=2024/month=02/notes.txt", OTHER_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_stage_files_rejects_bad_targets(workdir: Path, tmp_path: Path) -> None:
    with pytest.raises(server_client.BearpondError, match="no such file"):
        bearpond_client.BearpondClient(workdir).add([Path("nope.parquet")])

    elsewhere: Path = tmp_path / "elsewhere.csv"
    elsewhere.write_bytes(b"x")
    with pytest.raises(server_client.BearpondError, match="outside the working directory"):
        bearpond_client.BearpondClient(workdir).add([elsewhere])


# ----------------------------------------------------------------------------------------------------------------------
def test_stage_files_accepts_csv(workdir: Path) -> None:
    csv_path: Path = workdir / "year=2024/month=02/data.csv"
    csv_path.write_bytes(b"year,month,value\n2024,2,42")

    staged: list[types.FileMetadata] = bearpond_client.BearpondClient(workdir).add([Path("year=2024/month=02/data.csv")])
    assert [f.path for f in staged] == ["year=2024/month=02/data.csv"]


# ----------------------------------------------------------------------------------------------------------------------
def test_cmd_commit_end_to_end(workdir: Path, live_server: str, repo: repository.Repository) -> None:
    conftest.write_workspace_config(workdir, live_server)
    cli.cmd_add(argparse.Namespace(paths=[Path("year=2024")]))
    cli.cmd_commit(argparse.Namespace(token=None, message="first commit"))

    # the staging area is cleared, the lake has the files, and the commit message is on the record
    assert not (workdir / bearpond_client.STATE_DIR_NAME / bearpond_client.STAGED_NAME).exists()

    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert sorted(f.path for f in manifest.files) == [HIVE_PATH, OTHER_PATH]

    records: list[types.CommitRecord] = repo.metadata_store.get_commit_record_list()
    assert records[0].reason == "first commit"


# ----------------------------------------------------------------------------------------------------------------------
def test_cmd_commit_refuses_drifted_files(workdir: Path, live_server: str, repo: repository.Repository) -> None:
    conftest.write_workspace_config(workdir, live_server)
    cli.cmd_add(argparse.Namespace(paths=[Path(HIVE_PATH)]))
    (workdir / HIVE_PATH).write_bytes(b"changed after staging")

    with pytest.raises(server_client.BearpondError, match="modified since staged"):
        cli.cmd_commit(argparse.Namespace(token=None, message=None))

    # the staging area survives so the user can re-stage and retry, and nothing reached the server
    assert (workdir / bearpond_client.STATE_DIR_NAME / bearpond_client.STAGED_NAME).exists()
    assert conftest.current_manifest(repo) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_cmd_commit_with_nothing_staged(workdir: Path, live_server: str) -> None:
    conftest.write_workspace_config(workdir, live_server)
    with pytest.raises(server_client.BearpondError, match="nothing staged"):
        cli.cmd_commit(argparse.Namespace(token=None, message=None))


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_stages_removal_with_recorded_metadata(workdir: Path) -> None:
    workspace_files: dict = {HIVE_PATH: [conftest.sha256_hex(CONTENT), len(CONTENT)]}
    write_workspace_manifest(workdir, 3, workspace_files)

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
def test_rm_of_tracked_file_staged_for_readd_cancels_the_add(workdir: Path) -> None:
    # a tracked file staged for re-add: rm must cancel the pending add AND stage the removal — never both,
    # since the server rejects a manifest with the same path in added and removed
    write_workspace_manifest(workdir, 3, {HIVE_PATH: [conftest.sha256_hex(CONTENT), len(CONTENT)]})
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    workspace.add([Path(HIVE_PATH)])

    manifest: types.TransactionManifest = workspace.rm([Path(HIVE_PATH)])

    assert manifest.added == []
    assert [f.path for f in manifest.removed] == [HIVE_PATH]


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_commit_removes_from_lake_and_workspace(workdir: Path, live_server: str, repo: repository.Repository) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    with server_client.ServerClient(live_server, conftest.REPO_NAME) as server:
        workspace.add([Path("year=2024")])
        workspace.commit(server, message="add both")

        workspace.rm([Path(HIVE_PATH)])
        result: types.CommitResponse = workspace.commit(server, message="remove one")

    assert result.seq == 2

    # the lake, the workspace state, and the working directory all dropped the path
    manifest: types.Manifest | None = conftest.current_manifest(repo)
    assert manifest is not None
    assert [f.path for f in manifest.files] == [OTHER_PATH]
    assert sorted(workspace._tracked_files()) == [OTHER_PATH]
    assert workspace._tracked_manifest().seq == 2
    assert not (workdir / HIVE_PATH).exists()
    assert not (workdir / "year=2024/month=01").exists()
    assert (workdir / OTHER_PATH).read_bytes() == OTHER_CONTENT


# ----------------------------------------------------------------------------------------------------------------------
def test_mutating_operations_refuse_a_workspace_with_interrupted_sync(workdir: Path) -> None:
    # a dirty workspace (interrupted pull pending) only supports pull and status — add/rm/commit must refuse
    (workdir / bearpond_client.STATE_DIR_NAME / bearpond_client.PENDING_NAME).parent.mkdir(exist_ok=True)
    (workdir / bearpond_client.STATE_DIR_NAME / bearpond_client.PENDING_NAME).write_text(json.dumps({HIVE_PATH: [conftest.sha256_hex(CONTENT), len(CONTENT)]}))

    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    with pytest.raises(server_client.BearpondError, match="interrupted pull"):
        workspace.add([Path(HIVE_PATH)])
    with pytest.raises(server_client.BearpondError, match="interrupted pull"):
        workspace.rm([Path(HIVE_PATH)])
    with pytest.raises(server_client.BearpondError, match="interrupted pull"):
        workspace.commit(None)  # the guard fires before the server is ever touched

    # and status surfaces the pending state rather than hiding it — reported as pending, never as untracked
    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert status.pending == [HIVE_PATH]
    assert HIVE_PATH not in status.untracked


# ----------------------------------------------------------------------------------------------------------------------
def test_prune_empty_dirs_removes_empty_ancestors_but_keeps_files_and_root(tmp_path: Path) -> None:
    root: Path = tmp_path / "mirror"
    (root / "year=2024/month=01").mkdir(parents=True)
    (root / "year=2024/month=02").mkdir(parents=True)
    (root / "year=2024/month=02/part.parquet").write_bytes(b"data")

    # the deleted file's emptied chain is pruned up to root; the sibling tree and root itself survive
    bearpond_client.prune_empty_dirs(root, root / "year=2024/month=01/part.parquet")
    assert not (root / "year=2024/month=01").exists()
    assert (root / "year=2024/month=02/part.parquet").read_bytes() == b"data"
    assert root.is_dir()

    # a chain of nested empties prunes tip-to-root in one call, stopping at root
    bearpond_client.prune_empty_dirs(root, root / "a=b/c=d/gone.parquet")
    assert not (root / "a=b").exists()
    assert root.is_dir()


# ----------------------------------------------------------------------------------------------------------------------
def test_prune_empty_dirs_is_a_noop_outside_root(tmp_path: Path) -> None:
    # the confinement guarantee: a start outside root touches nothing, even empty directories
    root: Path = tmp_path / "mirror"
    root.mkdir()
    outside: Path = tmp_path / "outside" / "deep"
    outside.mkdir(parents=True)

    bearpond_client.prune_empty_dirs(root, outside / "file.parquet")

    assert outside.is_dir()


# ----------------------------------------------------------------------------------------------------------------------
def test_status_reports_staged_untracked_modified_and_missing(workdir: Path) -> None:
    # workspace state: PATH_A clean, OTHER_PATH tampered (hash differs), one path gone from disk entirely
    workspace_files: dict = {
        HIVE_PATH: [conftest.sha256_hex(CONTENT), len(CONTENT)],
        OTHER_PATH: ["0" * 64, len(OTHER_CONTENT)],
        "year=2024/month=03/gone.parquet": ["0" * 64, 1],
    }
    write_workspace_manifest(workdir, 7, workspace_files)

    # one file staged, one simply sitting on disk unknown to the lake
    (workdir / "year=2024/month=04").mkdir(parents=True)
    (workdir / "year=2024/month=04/new.parquet").write_bytes(b"new")
    (workdir / "year=2025").mkdir(parents=True)
    (workdir / "year=2025/stray.parquet").write_bytes(b"stray")

    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(workdir)
    workspace.add([Path("year=2024/month=04/new.parquet")])

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert status.workspace_seq == 7
    assert [f.path for f in status.staged_added] == ["year=2024/month=04/new.parquet"]
    assert status.untracked == ["year=2025/stray.parquet"]
    assert status.modified == [OTHER_PATH]
    assert status.missing == ["year=2024/month=03/gone.parquet"]
