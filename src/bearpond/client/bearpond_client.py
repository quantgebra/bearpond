import json
import os
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel

from .. import types
from . import server_client

WORKSPACE_STATE_NAME = "_workspace.json"
STAGED_NAME = "_staged.json"


# ======================================================================================================================
class FileState(NamedTuple):
    size: int
    sha256: str


# ======================================================================================================================
# the contents of _workspace.json: which manifest seq the workspace reflects, plus the per-file state at that seq
class WorkspaceState(NamedTuple):
    seq: int
    files: dict[str, FileState]


# ======================================================================================================================
# everything the workspace knows about how it differs from the lake, from status()
class WorkspaceStatus(BaseModel):
    workspace_seq: int
    staged_added: list[types.FileMetadata]
    staged_removed: list[types.FileMetadata]
    untracked: list[str]
    modified: list[str]
    missing: list[str]


# ----------------------------------------------------------------------------------------------------------------------
def prune_empty_dirs(root: Path, start: Path) -> None:
    # called after deleting a file during sync, so an emptied-out hive partition directory doesn't linger behind
    current: Path = start.parent
    while current != root and current.exists() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


# ======================================================================================================================
class BearpondClient:

    # the workspace facade: owns the local state of a working directory — which manifest seq it reflects
    # (_workspace.json), what's staged for the next commit (_staged.json), and how the files on disk differ from
    # both. Server interaction goes through a server_client.ServerClient handed to the methods that need it.

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, workdir: Path) -> None:
        self.workdir: Path = workdir

    # --- workspace state files ----------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def _load_workspace_state(self) -> WorkspaceState:
        # {"seq": N, "files": {path: [size, sha256]}} — two older on-disk formats still read: the pre-rename
        # _synced.json filename, and the pre-seq format (a bare path→[size, sha256] dict), which reads as seq 0
        state_path: Path = self.workdir / WORKSPACE_STATE_NAME
        state: WorkspaceState = WorkspaceState(seq=0, files={})
        if state_path.exists():
            raw: dict = json.loads(state_path.read_text())
            raw_files: dict[str, list] = raw["files"] if "files" in raw else raw
            seq: int = raw.get("seq", 0) if "files" in raw else 0
            files: dict[str, FileState] = {
                path: FileState(size=entry[0], sha256=entry[1]) for path, entry in raw_files.items()
            }
            state = WorkspaceState(seq=seq, files=files)
        return state

    # ------------------------------------------------------------------------------------------------------------------
    def _save_workspace_state(self, seq: int, files: dict[str, FileState]) -> None:
        # write to a temp file then rename over the real one, so a reader never observes a half-written state file
        state_path: Path = self.workdir / WORKSPACE_STATE_NAME
        tmp_path: Path = state_path.with_suffix(".tmp")
        raw: dict = {"seq": seq, "files": {path: [entry.size, entry.sha256] for path, entry in files.items()}}
        tmp_path.write_text(json.dumps(raw, indent=2))
        os.replace(tmp_path, state_path)

    # ------------------------------------------------------------------------------------------------------------------
    def _load_staged(self) -> types.TransactionManifest:
        # the client-side staging area (like git's index): what add() recorded, waiting for commit()
        staged_path: Path = self.workdir / STAGED_NAME
        manifest: types.TransactionManifest = types.TransactionManifest()
        if staged_path.exists():
            manifest = types.TransactionManifest.model_validate_json(staged_path.read_text())
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def _save_staged(self, manifest: types.TransactionManifest) -> None:
        staged_path: Path = self.workdir / STAGED_NAME
        tmp_path: Path = staged_path.with_suffix(".tmp")
        tmp_path.write_text(manifest.model_dump_json(indent=2))
        os.replace(tmp_path, staged_path)

    # --- read-only views ----------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def workspace_seq(self) -> int:
        return self._load_workspace_state().seq

    # ------------------------------------------------------------------------------------------------------------------
    def workspace_files(self) -> dict[str, FileState]:
        return self._load_workspace_state().files

    # ------------------------------------------------------------------------------------------------------------------
    def staged_manifest(self) -> types.TransactionManifest:
        return self._load_staged()

    # ------------------------------------------------------------------------------------------------------------------
    def status(self) -> WorkspaceStatus:
        state: WorkspaceState = self._load_workspace_state()
        seq: int = state.seq
        workspace_files: dict[str, FileState] = state.files
        staged: types.TransactionManifest = self._load_staged()
        staged_paths: set[str] = {f.path for f in staged.added} | {f.path for f in staged.removed}

        untracked: list[str] = []
        modified: list[str] = []
        missing: list[str] = []
        if self.workdir.exists():
            for path in sorted(self.workdir.rglob("*.parquet")):
                rel_path: str = path.relative_to(self.workdir).as_posix()
                if rel_path not in workspace_files and rel_path not in staged_paths:
                    untracked.append(rel_path)
                elif rel_path in workspace_files:
                    # drift from what the workspace has recorded: size short-circuits, hash only when the size still matches
                    recorded: FileState = workspace_files[rel_path]
                    if path.stat().st_size != recorded.size:
                        modified.append(rel_path)
                    else:
                        sha256, _ = server_client.hash_and_size(path)
                        if sha256 != recorded.sha256:
                            modified.append(rel_path)
        for rel_path in workspace_files:
            if not (self.workdir / rel_path).is_file() and rel_path not in staged_paths:
                missing.append(rel_path)

        return WorkspaceStatus(
            workspace_seq=seq,
            staged_added=staged.added,
            staged_removed=staged.removed,
            untracked=untracked,
            modified=modified,
            missing=missing,
        )

    # --- staging ------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def add(self, targets: list[Path]) -> list[types.FileMetadata]:
        # stages each target for the next commit: directories expand to the .parquet files beneath them, and a
        # file's lake path is its location relative to the workspace root. Re-adding a path updates its entry.
        resolved: list[Path] = []
        for target in targets:
            # resolve up front so the relative_to() check below works whether targets are relative or absolute
            target = target.resolve()
            if target.is_dir():
                resolved.extend(sorted(target.rglob("*.parquet")))
            elif target.is_file():
                resolved.append(target)
            else:
                raise server_client.BearpondError(f"no such file or directory: {target}")

        manifest: types.TransactionManifest = self._load_staged()
        staged: dict[str, types.FileMetadata] = {f.path: f for f in manifest.added}

        newly_staged: list[types.FileMetadata] = []
        for path in resolved:
            try:
                rel_path: str = path.relative_to(self.workdir.resolve()).as_posix()
            except ValueError:
                raise server_client.BearpondError(f"path is outside the working directory: {path}")
            try:
                types.validate_hive_parquet_path(rel_path)
            except ValueError as e:
                raise server_client.BearpondError(str(e))

            sha256, size = server_client.hash_and_size(path)
            entry: types.FileMetadata = types.FileMetadata(path=rel_path, size=size, sha256=sha256)
            staged[rel_path] = entry
            newly_staged.append(entry)

        self._save_staged(types.TransactionManifest(added=list(staged.values()), removed=manifest.removed))
        return newly_staged

    # ------------------------------------------------------------------------------------------------------------------
    def rm(self, targets: list[Path]) -> types.TransactionManifest:
        # stages each target for removal in the next commit. The removal entry carries the workspace's recorded metadata — the
        # exact content the workspace last observed — which is what the server's safe-removal check compares
        # against, so rm works offline and regardless of the file's current state on disk. Removing a path that
        # is staged for addition but not yet committed simply unstage it: there is nothing in the lake to remove.
        workspace_files: dict[str, FileState] = self._load_workspace_state().files
        manifest: types.TransactionManifest = self._load_staged()
        staged_added: dict[str, types.FileMetadata] = {f.path: f for f in manifest.added}
        staged_removed: dict[str, types.FileMetadata] = {f.path: f for f in manifest.removed}

        for target in targets:
            target = target.resolve()
            try:
                rel_path: str = target.relative_to(self.workdir.resolve()).as_posix()
            except ValueError:
                raise server_client.BearpondError(f"path is outside the working directory: {target}")

            if rel_path in staged_added and rel_path not in workspace_files:
                del staged_added[rel_path]
            elif rel_path not in workspace_files:
                raise server_client.BearpondError(f"not tracked by the workspace: {rel_path}")
            else:
                observed: FileState = workspace_files[rel_path]
                staged_removed[rel_path] = types.FileMetadata(
                    path=rel_path, size=observed.size, sha256=observed.sha256
                )

        result: types.TransactionManifest = types.TransactionManifest(
            added=list(staged_added.values()), removed=list(staged_removed.values())
        )
        self._save_staged(result)
        return result

    # --- server-driven workflows --------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, server: server_client.ServerClient, message: str | None = None) -> types.CommitResponse:
        # the one short-lived server interaction: begin, upload everything staged, commit — all in one go
        manifest: types.TransactionManifest = self._load_staged()
        if not manifest.added and not manifest.removed:
            raise server_client.BearpondError("nothing staged to commit — stage files with `bearpond add` first")

        # the staged metadata must still match what's on disk — re-hash everything and refuse on any drift
        files: dict[str, Path] = {f.path: self.workdir / f.path for f in manifest.added}
        for rel_path, local_path in files.items():
            if not local_path.is_file():
                raise server_client.BearpondError(f"staged file is missing: {rel_path} (re-stage with `bearpond add`)")
        staged_by_path: dict[str, types.FileMetadata] = {f.path: f for f in manifest.added}
        current: list[types.FileMetadata] = server_client.ServerClient.declare_files(files)
        drifted: list[str] = [f.path for f in current if staged_by_path[f.path].sha256 != f.sha256]
        if drifted:
            raise server_client.BearpondError(f"modified since staged — re-stage with `bearpond add`: {drifted}")

        txn_uuid: str = server.begin_transaction(current, manifest.removed)
        result: types.CommitResponse
        try:
            server.upload_files(txn_uuid, files)
            result = server.commit(txn_uuid, reason=message)
        except Exception:
            # best-effort cleanup — an upload failure or interrupted commit shouldn't leave an orphaned transaction
            server.abort(txn_uuid)
            raise

        # the workspace now reflects exactly what the lake holds at this seq — committed additions join the workspace
        # state, and removed paths leave both the workspace state and the working directory
        (self.workdir / STAGED_NAME).unlink(missing_ok=True)
        workspace_files: dict[str, FileState] = self._load_workspace_state().files
        for f in current:
            workspace_files[f.path] = FileState(size=f.size, sha256=f.sha256)
        for f in manifest.removed:
            workspace_files.pop(f.path, None)
            doomed: Path = self.workdir / f.path
            doomed.unlink(missing_ok=True)
            prune_empty_dirs(self.workdir, doomed)
        self._save_workspace_state(result.seq, workspace_files)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def sync(self, server: server_client.ServerClient, dry_run: bool = False) -> None:
        # let's make sure that the workdir exists
        self.workdir.mkdir(parents=True, exist_ok=True)

        # get the manifest from the server
        manifest: types.Manifest = server.get_manifest()
        manifest_files: dict[str, FileState] = {
            f.path: FileState(size=f.size, sha256=f.sha256) for f in manifest.files
        }
        
        # get the workspace seq and file metadata
        synced_state: WorkspaceState = self._load_workspace_state()
        workspace_files: dict[str, FileState] = synced_state.files

        to_download: list = [p for p in manifest_files if workspace_files.get(p) != manifest_files[p]]
        to_delete: list = [p for p in workspace_files if p not in manifest_files]

        print(
            f"manifest-{manifest.seq:08d}: {len(manifest_files)} files "
            f"({len(to_download)} to fetch, {len(to_delete)} to prune)"
        )

        if dry_run:
            for p in to_download:
                print(f"  would fetch: {p}")
            for p in to_delete:
                print(f"  would prune: {p}")
        else:
            # state is saved after every single file so a crash or dropped connection mid-sync loses at most the
            # file in flight — and the seq only advances to the new manifest once it is fully reflected on disk
            state: dict[str, FileState] = dict(workspace_files)

            for rel_path in to_download:
                dest_path: Path = self.workdir / rel_path
                expected: FileState = manifest_files[rel_path]
                actual_sha256: str = server.download_file(rel_path, dest_path)
                if actual_sha256 != expected.sha256:
                    dest_path.unlink(missing_ok=True)
                    raise server_client.BearpondError(f"checksum mismatch after fetching {rel_path}")
                state[rel_path] = FileState(size=expected.size, sha256=actual_sha256)
                self._save_workspace_state(synced_state.seq, state)
                print(f"  fetched: {rel_path}")

            for rel_path in to_delete:
                dest_path = self.workdir / rel_path
                dest_path.unlink(missing_ok=True)
                prune_empty_dirs(self.workdir, dest_path)
                del state[rel_path]
                self._save_workspace_state(synced_state.seq, state)
                print(f"  pruned: {rel_path}")

            self._save_workspace_state(manifest.seq, state)
            print(
                f"Synced to manifest-{manifest.seq:08d}: "
                f"+{len(to_download)} -{len(to_delete)} (total {len(state)} files)"
            )
