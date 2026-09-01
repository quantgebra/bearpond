import argparse
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import conftest
from bearpond import types
from bearpond.client import bearpond_client
from bearpond.client import cli
from bearpond.client import server_client
from bearpond.server import repository

PATH_A = "year=2024/month=01/a.parquet"
PATH_B = "year=2024/month=02/b.parquet"
CONTENT_A = b"alpha parquet bytes"
CONTENT_B = b"beta parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    # a local tree mirroring the target hive layout, like the kind a workspace's add would stage
    root: Path = tmp_path / "source"
    (root / PATH_A).parent.mkdir(parents=True)
    (root / PATH_B).parent.mkdir(parents=True)
    (root / PATH_A).write_bytes(CONTENT_A)
    (root / PATH_B).write_bytes(CONTENT_B)
    return root


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(live_server: str) -> Iterator[server_client.ServerClient]:
    with server_client.ServerClient(live_server, conftest.REPO_NAME) as active_client:
        yield active_client


# ----------------------------------------------------------------------------------------------------------------------
def commit_source_dir(client: server_client.ServerClient, source_dir: Path) -> types.CommitResponse:
    # drives the full transport-level write lifecycle: begin -> upload -> commit
    files: dict[str, Path] = {
        path.relative_to(source_dir).as_posix(): path
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_uuid: types.TxnUuid = client.begin_transaction(types.TransactionManifest(added=declared))
    client.upload_files(txn_uuid, files)
    return client.commit(txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def commit_file(client: server_client.ServerClient, source_dir: Path, rel_path: str) -> types.CommitResponse:
    # drives a single file through the full write lifecycle as its own commit
    files: dict[str, Path] = {rel_path: source_dir / rel_path}
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_uuid: types.TxnUuid = client.begin_transaction(types.TransactionManifest(added=declared))
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
def test_pull_downloads_everything_and_records_state(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client)

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert (target / PATH_B).read_bytes() == CONTENT_B

    state: dict[str, bearpond_client.FileState] = workspace._tracked_files()
    assert sorted(state) == [PATH_A, PATH_B]
    assert state[PATH_A].sha256 == conftest.sha256_hex(CONTENT_A)
    assert workspace._tracked_manifest().seq == 1


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
    workspace.pull(client)

    downloads: list[str] = []
    original: Callable[[str, Path], str] = client.download_file

    def counting_download(rel_path: str, dest_path: Path) -> str:
        downloads.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", counting_download)
    workspace.pull(client)
    assert downloads == []


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_prunes_removed_files_and_empty_dirs(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client)

    # a second transaction removes PATH_A from the lake, against the exact content the client observed
    removed_meta: types.FileMetadata = conftest.file_meta(PATH_A, CONTENT_A)
    txn_uuid: types.TxnUuid = client.begin_transaction(types.TransactionManifest(removed=[removed_meta]))
    client.commit(txn_uuid)

    workspace.pull(client)

    assert not (target / PATH_A).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B
    # the emptied month=01 partition directory is pruned, while month=02's hierarchy stays
    assert not (target / "year=2024/month=01").exists()
    assert (target / "year=2024/month=02").is_dir()

    state: dict[str, bearpond_client.FileState] = workspace._tracked_files()
    assert sorted(state) == [PATH_B]
    assert workspace._tracked_manifest().seq == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_fails_on_checksum_mismatch(
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
        workspace.pull(client)


# ----------------------------------------------------------------------------------------------------------------------
def test_get_transaction_status(client: server_client.ServerClient, source_dir: Path) -> None:
    files: dict[str, Path] = {PATH_A: source_dir / PATH_A}
    declared: list[types.FileMetadata] = client.declare_files(files)
    txn_uuid: types.TxnUuid = client.begin_transaction(types.TransactionManifest(added=declared))
    assert client.get_transaction_status(txn_uuid).status == "open"

    client.upload_files(txn_uuid, files)
    client.commit(txn_uuid)

    status: types.TransactionStatus = client.get_transaction_status(txn_uuid)
    assert status.status == "committed"
    assert status.seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_clone_creates_a_bound_workspace_with_files(
    client: server_client.ServerClient, source_dir: Path, live_server: str, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    # clone is the only way a workspace comes into being: remote bound, first pull done
    target: Path = tmp_path / "clone-of-testrepo"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient.clone(
        live_server, conftest.REPO_NAME, target
    )

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert (target / PATH_B).read_bytes() == CONTENT_B
    manifest: types.Manifest | None = workspace._tracked_manifest()
    assert manifest is not None and manifest.seq == 1
    config: dict = json.loads((target / ".bearpond" / "config.json").read_text())
    assert config == {"server": live_server, "repo": conftest.REPO_NAME}


# ----------------------------------------------------------------------------------------------------------------------
def test_clone_refuses_non_empty_target_and_cleans_up_on_failure(live_server: str, tmp_path: Path) -> None:
    # a non-empty target is refused outright
    target: Path = tmp_path / "occupied"
    target.mkdir()
    (target / "something.txt").write_text("x")
    with pytest.raises(server_client.BearpondError, match="not empty"):
        bearpond_client.BearpondClient.clone(live_server, conftest.REPO_NAME, target)

    # cloning a repo that doesn't exist fails and leaves nothing behind
    missing: Path = tmp_path / "ghost"
    with pytest.raises(server_client.BearpondError):
        bearpond_client.BearpondClient.clone(live_server, "no-such-repo", missing)
    assert not missing.exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_cli_repo_create_then_clone(live_server: str, tmp_path: Path) -> None:
    # the full onboarding flow: create a repo on the server, then clone it into a workspace
    cli.cmd_repo_create(argparse.Namespace(url=f"{live_server}/newrepo", token=None))

    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient.clone(
        live_server, "newrepo", tmp_path / "newrepo"
    )
    manifest: types.Manifest | None = workspace._tracked_manifest()
    assert manifest is not None and manifest.seq == 0  # a fresh repo is an empty lake


# ----------------------------------------------------------------------------------------------------------------------
def test_get_commit_history_and_cli_log(
    client: server_client.ServerClient,
    source_dir: Path,
    live_server: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A])
    workspace.commit(client, message="first trades")

    page: types.CommitHistoryPage = client.get_commit_history(limit=5)
    assert [r.seq for r in page.records] == [1]
    assert page.records[0].reason == "first trades"
    assert page.records[0].parent_commit_hash is None

    # the CLI prints the git-style log from inside a workspace
    workdir: Path = tmp_path / "ws"
    conftest.write_workspace_config(workdir, live_server)
    monkeypatch.chdir(workdir)
    cli.cmd_log(argparse.Namespace(token=None, limit=5))
    out: str = capsys.readouterr().out
    assert "manifest-00000001" in out
    assert "first trades" in out
    assert "+1 -0 ~0 file(s)" in out


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_stores_the_pristine_manifest(client: server_client.ServerClient, source_dir: Path, tmp_path: Path) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    bearpond_client.BearpondClient(target).pull(client)

    # the manifest file holds exactly what the server served — field-for-field, not a re-derivation
    raw: dict = json.loads((target / bearpond_client.STATE_DIR_NAME / bearpond_client.MANIFEST_NAME).read_text())
    assert raw == client.get_manifest().model_dump()
    assert not (target / bearpond_client.STATE_DIR_NAME / bearpond_client.PENDING_NAME).exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_stores_the_pristine_manifest(client: server_client.ServerClient, source_dir: Path) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A, source_dir / PATH_B])
    result: types.CommitResponse = workspace.commit(client, message="initial")

    raw: dict = json.loads((source_dir / bearpond_client.STATE_DIR_NAME / bearpond_client.MANIFEST_NAME).read_text())
    assert raw["seq"] == result.seq == 1
    assert not (source_dir / bearpond_client.STATE_DIR_NAME / bearpond_client.PENDING_NAME).exists()
    assert workspace._tracked_manifest().seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_resumes_from_pending_after_interruption(
    client: server_client.ServerClient,
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_source_dir(client, source_dir)
    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)

    # a pull that dies after the first download: the completed file is recorded in the pending overlay
    original: Callable[[str, Path], str] = client.download_file
    calls: list[str] = []

    def fail_after_first(rel_path: str, dest_path: Path) -> str:
        if calls:
            raise server_client.BearpondError("boom")
        calls.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", fail_after_first)
    with pytest.raises(server_client.BearpondError, match="boom"):
        workspace.pull(client)

    # no manifest yet; the pending file marks the interrupted pull and records the completed download
    assert not (target / bearpond_client.STATE_DIR_NAME / bearpond_client.MANIFEST_NAME).exists()
    pending: dict = json.loads((target / bearpond_client.STATE_DIR_NAME / bearpond_client.PENDING_NAME).read_text())
    assert list(pending) == [PATH_A]

    # the retry downloads only what the overlay doesn't already cover, then adopts the full manifest
    def recording(rel_path: str, dest_path: Path) -> str:
        calls.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", recording)
    calls.clear()
    workspace.pull(client)

    assert calls == [PATH_B]
    final: dict = json.loads((target / bearpond_client.STATE_DIR_NAME / bearpond_client.MANIFEST_NAME).read_text())
    assert final["seq"] == 1
    assert not (target / bearpond_client.STATE_DIR_NAME / bearpond_client.PENDING_NAME).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_by_seq(client: server_client.ServerClient, source_dir: Path) -> None:
    commit_file(client, source_dir, PATH_A)
    commit_file(client, source_dir, PATH_B)

    first: types.Manifest = client.get_manifest(seq=1)
    assert [f.path for f in first.files] == [PATH_A]

    second: types.Manifest = client.get_manifest(seq=2)
    assert [f.path for f in second.files] == [PATH_A, PATH_B]


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_to_specific_seq_downloads_only_that_version(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_file(client, source_dir, PATH_A)
    commit_file(client, source_dir, PATH_B)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, seq=1)

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert not (target / PATH_B).exists()
    assert workspace._tracked_manifest().seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_to_specific_seq_prunes_files_from_a_newer_version(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_file(client, source_dir, PATH_A)
    commit_file(client, source_dir, PATH_B)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client)
    assert (target / PATH_B).exists()

    workspace.pull(client, seq=1)
    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert not (target / PATH_B).exists()
    assert workspace._tracked_manifest().seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_to_specific_seq_refuses_with_pending_pull(
    client: server_client.ServerClient,
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_file(client, source_dir, PATH_A)
    commit_file(client, source_dir, PATH_B)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)

    original: Callable[[str, Path], str] = client.download_file
    calls: list[str] = []

    def fail_after_first(rel_path: str, dest_path: Path) -> str:
        if calls:
            raise server_client.BearpondError("boom")
        calls.append(rel_path)
        return original(rel_path, dest_path)

    monkeypatch.setattr(client, "download_file", fail_after_first)
    with pytest.raises(server_client.BearpondError, match="boom"):
        workspace.pull(client)

    monkeypatch.setattr(client, "download_file", original)
    with pytest.raises(server_client.BearpondError, match="interrupted pull"):
        workspace.pull(client, seq=1)


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_switching_view_refuses_with_staged_changes(
    client: server_client.ServerClient,
    source_dir: Path,
    tmp_path: Path,
) -> None:
    commit_file(client, source_dir, PATH_A)
    commit_file(client, source_dir, PATH_B)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client)

    # stage an update against the current full-mirror view
    new_content: bytes = b"updated alpha parquet bytes"
    (target / PATH_A).write_bytes(new_content)
    workspace.add([target / PATH_A])

    # switching the query or seq is rejected while staged changes exist
    with pytest.raises(server_client.BearpondError, match="staged changes"):
        workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=02"]]))

    with pytest.raises(server_client.BearpondError, match="staged changes"):
        workspace.pull(client, seq=1)

    # but a plain re-sync of the current view is still allowed
    workspace.pull(client)


# ----------------------------------------------------------------------------------------------------------------------
def test_cli_pull_with_seq(
    client: server_client.ServerClient,
    source_dir: Path,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_file(client, source_dir, PATH_A)
    commit_file(client, source_dir, PATH_B)

    workdir: Path = tmp_path / "ws"
    bearpond_client.BearpondClient.clone(live_server, conftest.REPO_NAME, workdir)

    monkeypatch.chdir(workdir)
    cli.cmd_pull(argparse.Namespace(token=None, dry_run=False, seq=1, query=None))

    assert (workdir / PATH_A).read_bytes() == CONTENT_A
    assert not (workdir / PATH_B).exists()
    assert bearpond_client.BearpondClient(workdir)._tracked_manifest().seq == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_add_on_tracked_file_stages_update(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A, source_dir / PATH_B])
    workspace.commit(client, message="initial")

    # change PATH_A on disk and re-add it — this should stage an update, not a conflicting add
    new_content: bytes = b"updated alpha parquet bytes"
    (source_dir / PATH_A).write_bytes(new_content)
    workspace.add([source_dir / PATH_A])

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert len(status.staged_updated) == 1
    assert status.staged_updated[0].path == PATH_A
    assert status.staged_updated[0].new_sha256 == conftest.sha256_hex(new_content)
    assert not status.staged_added
    assert not status.staged_removed


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_update_replaces_file_on_server(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A, source_dir / PATH_B])
    workspace.commit(client, message="initial")

    new_content: bytes = b"updated alpha parquet bytes"
    (source_dir / PATH_A).write_bytes(new_content)
    workspace.add([source_dir / PATH_A])

    result: types.CommitResponse = workspace.commit(client, message="update alpha")
    assert result.seq == 2
    assert result.files_added_count == 0
    assert result.files_removed_count == 0
    assert result.files_updated_count == 1

    manifest: types.Manifest = client.get_manifest()
    assert [f.path for f in manifest.files] == [PATH_A, PATH_B]
    manifest_by_path: dict[str, bearpond_client.FileState] = {}
    for f in manifest.files:
        manifest_by_path[f.path] = bearpond_client.FileState(size=f.size, sha256=f.sha256)
    assert manifest_by_path[PATH_A].sha256 == conftest.sha256_hex(new_content)
    assert manifest_by_path[PATH_B].sha256 == conftest.sha256_hex(CONTENT_B)


# ----------------------------------------------------------------------------------------------------------------------
def test_status_does_not_show_modified_for_staged_update(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A, source_dir / PATH_B])
    workspace.commit(client, message="initial")

    (source_dir / PATH_A).write_bytes(b"updated alpha parquet bytes")
    workspace.add([source_dir / PATH_A])

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert PATH_A not in status.modified
    assert len(status.staged_updated) == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_rm_cancels_pending_update(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    workspace.add([source_dir / PATH_A, source_dir / PATH_B])
    workspace.commit(client, message="initial")

    (source_dir / PATH_A).write_bytes(b"updated alpha parquet bytes")
    workspace.add([source_dir / PATH_A])
    workspace.rm([source_dir / PATH_A])

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert not status.staged_updated
    assert len(status.staged_removed) == 1
    assert status.staged_removed[0].path == PATH_A


# ----------------------------------------------------------------------------------------------------------------------
def test_cli_update_flow(
    client: server_client.ServerClient,
    source_dir: Path,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # CLI commands need a cloned workspace with config.json, not a bare BearpondClient
    workdir: Path = tmp_path / "ws"
    bearpond_client.BearpondClient.clone(live_server, conftest.REPO_NAME, workdir)

    # seed the repo by committing from the source directory, then pull the workspace up to date
    seed: bearpond_client.BearpondClient = bearpond_client.BearpondClient(source_dir)
    seed.add([source_dir / PATH_A, source_dir / PATH_B])
    seed.commit(client, message="initial")

    bearpond_client.BearpondClient(workdir).pull(client)

    (workdir / PATH_A).write_bytes(b"updated alpha parquet bytes")

    monkeypatch.chdir(workdir)
    cli.cmd_add(argparse.Namespace(paths=[workdir / PATH_A]))
    cli.cmd_status(argparse.Namespace())
    out: str = capsys.readouterr().out
    assert "staged update" in out

    cli.cmd_commit(argparse.Namespace(token=None, message="update via cli"))
    out = capsys.readouterr().out
    assert "~1 file(s)" in out

    assert bearpond_client.BearpondClient(workdir)._tracked_manifest().seq == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_with_query_downloads_only_matching_files(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=01"]]))

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert not (target / PATH_B).exists()

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert status.query.groups == [["month=01"]]
    assert PATH_A not in status.missing
    assert PATH_B not in status.missing  # outside the query view
    assert PATH_B not in status.untracked  # outside the query view


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_with_multiple_queries_ors_groups(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=01"], ["month=02"]]))

    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert (target / PATH_B).read_bytes() == CONTENT_B
    assert workspace.status().query.groups == [["month=01"], ["month=02"]]


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_switching_query_prunes_files_from_old_view(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=01"]]))
    assert (target / PATH_A).exists()
    assert not (target / PATH_B).exists()

    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=02"]]))
    assert not (target / PATH_A).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_full_mirror_after_subset_restores_all_files(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=01"]]))
    assert not (target / PATH_B).exists()

    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[]))
    assert (target / PATH_A).read_bytes() == CONTENT_A
    assert (target / PATH_B).read_bytes() == CONTENT_B


# ----------------------------------------------------------------------------------------------------------------------
def test_pull_query_leaves_user_files_outside_view_untouched(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=01"]]))

    # a user drops an unrelated file into the workspace — it should survive a query switch
    extra_path: str = "year=2024/month=03/extra.parquet"
    extra_file: Path = target / extra_path
    extra_file.parent.mkdir(parents=True)
    extra_file.write_bytes(b"user file")

    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=02"]]))
    assert not (target / PATH_A).exists()
    assert (target / PATH_B).read_bytes() == CONTENT_B
    assert extra_file.read_bytes() == b"user file"


# ----------------------------------------------------------------------------------------------------------------------
def test_status_shows_current_query(
    client: server_client.ServerClient, source_dir: Path, tmp_path: Path
) -> None:
    commit_source_dir(client, source_dir)

    target: Path = tmp_path / "mirror"
    workspace: bearpond_client.BearpondClient = bearpond_client.BearpondClient(target)
    workspace.pull(client, query=bearpond_client.SubsetQuery(groups=[["month=01"]]))

    status: bearpond_client.WorkspaceStatus = workspace.status()
    assert status.query.groups == [["month=01"]]


# ----------------------------------------------------------------------------------------------------------------------
def test_cli_pull_with_query(
    client: server_client.ServerClient,
    source_dir: Path,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_source_dir(client, source_dir)

    workdir: Path = tmp_path / "ws"
    bearpond_client.BearpondClient.clone(live_server, conftest.REPO_NAME, workdir)

    monkeypatch.chdir(workdir)
    cli.cmd_pull(argparse.Namespace(token=None, dry_run=False, seq=None, query=[["month=01"]]))

    assert (workdir / PATH_A).read_bytes() == CONTENT_A
    assert not (workdir / PATH_B).exists()
    assert bearpond_client.BearpondClient(workdir).status().query.groups == [["month=01"]]
