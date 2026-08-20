import json
import os
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel

from .. import types
from . import server_client

MANIFEST_NAME = "_manifest.json"
PENDING_NAME = "_pending.json"
STAGED_NAME = "_staged.json"


# ======================================================================================================================
class FileState(NamedTuple):
    size: int
    sha256: str


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

    # the workspace facade: owns the local state of a working directory — which manifest it represents
    # (_manifest.json, a pristine copy of what the server served), sync progress if one died partway
    # (_pending.json), what's staged for the next commit (_staged.json), and how the files on disk differ from
    # all of it. Server interaction goes through a server_client.ServerClient handed to the methods that need it.

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, workdir: Path) -> None:
        self.workdir: Path = workdir

    # --- workspace state files ----------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def _write_state_file(self, name: str, content: str) -> None:
        # write to a temp file then rename over the real one, so a reader never observes a half-written state file
        path: Path = self.workdir / name
        tmp_path: Path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.replace(tmp_path, path)

    # ------------------------------------------------------------------------------------------------------------------
    def _load_manifest(self) -> types.Manifest | None:
        # the pristine copy of the server manifest this workspace claims to represent — None if never synced
        manifest: types.Manifest | None = None
        manifest_path: Path = self.workdir / MANIFEST_NAME
        if manifest_path.exists():
            manifest = types.Manifest.model_validate_json(manifest_path.read_text())
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def _save_manifest(self, manifest: types.Manifest) -> None:
        self._write_state_file(MANIFEST_NAME, manifest.model_dump_json(indent=2))

    # ------------------------------------------------------------------------------------------------------------------
    def _load_pending(self) -> dict[str, FileState]:
        # progress of an interrupted sync — the file only exists when a sync died partway through
        pending: dict[str, FileState] = {}
        pending_path: Path = self.workdir / PENDING_NAME
        if pending_path.exists():
            raw: dict[str, list] = json.loads(pending_path.read_text())
            pending = {path: FileState(size=entry[0], sha256=entry[1]) for path, entry in raw.items()}
        return pending

    # ------------------------------------------------------------------------------------------------------------------
    def _save_pending(self, pending: dict[str, FileState]) -> None:
        raw: dict = {path: [entry.size, entry.sha256] for path, entry in pending.items()}
        self._write_state_file(PENDING_NAME, json.dumps(raw, indent=2))

    # ------------------------------------------------------------------------------------------------------------------
    def _workspace_files(self) -> dict[str, FileState]:
        # the effective view: the manifest's files with the pending overlay applied
        manifest: types.Manifest | None = self._load_manifest()
        files: dict[str, FileState] = (
            {f.path: FileState(size=f.size, sha256=f.sha256) for f in manifest.files} if manifest else {}
        )
        files.update(self._load_pending())
        return files

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
        self._write_state_file(STAGED_NAME, manifest.model_dump_json(indent=2))

    # --- read-only views ----------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def workspace_seq(self) -> int:
        manifest: types.Manifest | None = self._load_manifest()
        return manifest.seq if manifest is not None else 0

    # ------------------------------------------------------------------------------------------------------------------
    def workspace_files(self) -> dict[str, FileState]:
        return self._workspace_files()

    # ------------------------------------------------------------------------------------------------------------------
    def staged_manifest(self) -> types.TransactionManifest:
        return self._load_staged()

    # ------------------------------------------------------------------------------------------------------------------
    def status(self) -> WorkspaceStatus:
        manifest_for_seq: types.Manifest | None = self._load_manifest()
        seq: int = manifest_for_seq.seq if manifest_for_seq is not None else 0
        workspace_files: dict[str, FileState] = self._workspace_files()
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
        workspace_files: dict[str, FileState] = self._workspace_files()
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

        # the workspace now represents the seq this commit produced — fetch the manifest verbatim from the server
        # (the pristine copy) rather than reconstructing it locally, which also confirms the commit landed as reported
        new_manifest: types.Manifest = server.get_manifest()
        if new_manifest.seq != result.seq:
            raise server_client.BearpondError(
                f"commit reported manifest seq {result.seq} but the server is at seq {new_manifest.seq}"
            )

        (self.workdir / STAGED_NAME).unlink(missing_ok=True)
        for f in manifest.removed:
            doomed: Path = self.workdir / f.path
            doomed.unlink(missing_ok=True)
            prune_empty_dirs(self.workdir, doomed)
        self._save_manifest(new_manifest)
        (self.workdir / PENDING_NAME).unlink(missing_ok=True)
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
        
        # get the workspace manifes and the workspace files
        # the workspace files are the ones from the current manifest as well as from an interrupted sync
        workspace_manifest: types.Manifest | None = self._load_manifest()
        workspace_files: dict[str, FileState] = self._workspace_files()

        # calculate file actions
        to_download: list = [p for p in manifest_files if workspace_files.get(p) != manifest_files[p]]
        to_delete: list = [p for p in workspace_files if p not in manifest_files]

        print(
            f"manifest-{manifest.seq:08d}: {len(manifest_files)} files "
            f"({len(to_download)} to download, {len(to_delete)} to delete)"
        )

        if dry_run:
            for p in to_download:
                print(f"  would download: {p}")
            for p in to_delete:
                print(f"  would delete: {p}")
        else:
            # progress is saved after every single file so a crash mid-sync loses at most the file in flight: the
            # workspace keeps its previous manifest while a "pending" overlay accumulates downloaded files, and it
            # only adopts the new manifest once it is fully reflected on disk. Deletions need no progress tracking —
            # they're recomputable from the manifest diff and idempotent to redo.
            pending: dict[str, FileState] = {}

            # download files
            for rel_path in to_download:
                dest_path: Path = self.workdir / rel_path
                expected_file_state: FileState = manifest_files[rel_path]
                
                # test the file sha256
                actual_sha256: str = server.download_file(rel_path, dest_path)
                if actual_sha256 != expected_file_state.sha256:
                    dest_path.unlink(missing_ok=True)
                    raise server_client.BearpondError(f"checksum mismatch after fetching {rel_path}")
                
                # update and store pending
                pending[rel_path] = FileState(size=expected_file_state.size, sha256=actual_sha256)
                self._save_pending(pending)
                print(f"  fetched: {rel_path}")

            # delete files
            for rel_path in to_delete:
                dest_path = self.workdir / rel_path
                dest_path.unlink(missing_ok=True)
                prune_empty_dirs(self.workdir, dest_path)
                print(f"  pruned: {rel_path}")

            self._save_manifest(manifest)
            (self.workdir / PENDING_NAME).unlink(missing_ok=True)
            print(
                f"Synced to manifest-{manifest.seq:08d}: "
                f"+{len(to_download)} -{len(to_delete)} (total {len(manifest.files)} files)"
            )
