import json
import os
import shutil
from pathlib import Path
from typing import NamedTuple, Optional

from pydantic import BaseModel

from .. import types
from . import server_client

STATE_DIR_NAME = ".bearpond"
MANIFEST_NAME = "manifest.json"
PENDING_NAME = "pending.json"
STAGED_NAME = "staged.json"
CONFIG_NAME = "config.json"


# ======================================================================================================================
class FileState(NamedTuple):
    size: int
    sha256: str


# ======================================================================================================================
# the remote binding recorded at clone time (.bearpond/config.json)
class WorkspaceConfig(BaseModel):
    server: str
    repo: str


# ======================================================================================================================
# everything the workspace knows about how it differs from the lake, from status()
class WorkspaceStatus(BaseModel):
    workspace_seq: int
    pending: list[str]
    staged_added: list[types.FileMetadata]
    staged_removed: list[types.FileMetadata]
    untracked: list[str]
    modified: list[str]
    missing: list[str]


# ----------------------------------------------------------------------------------------------------------------------
def prune_empty_dirs(root: Path, start: Path) -> None:
    # prunes start's ancestor chain tip-to-root, stopping at the first non-empty directory and never at root
    # itself. Confinement is by path check, not by traversal or exception: is_relative_to is the playing field,
    # so a start outside root makes this a no-op — nothing outside root can ever be touched. No exceptions are
    # used for control flow: emptiness is checked before every delete.
    resolved_root: Path = root.resolve()
    current: Path = start.resolve().parent
    while current != resolved_root and current.is_relative_to(resolved_root):
        if not current.is_dir():
            # a missing directory satisfies the prune intent already — keep walking up
            current = current.parent
            continue
        if any(current.iterdir()):
            break
        current.rmdir()
        current = current.parent


# ======================================================================================================================
class BearpondClient:
    
    # the workspace facade: owns the local state of a working directory — the remote it was cloned from
    # (.bearpond/config.json), which manifest it represents (.bearpond/manifest.json, a pristine copy of what the
    # server served), pull progress if one died partway (.bearpond/pending.json), what's staged for the next
    # commit (.bearpond/staged.json), and how the files on disk differ from all of it. Server interaction goes
    # through a server_client.ServerClient handed to the methods that need it.
    
    # ------------------------------------------------------------------------------------------------------------------
    @classmethod
    def clone(cls, server_url: str, repo: str, target: Path, token: str | None = None) -> "BearpondClient":
        # the only way a workspace comes into being: bound to a remote repo from birth, first pull included
        if target.exists() and any(target.iterdir()):
            raise server_client.BearpondError(f"target directory is not empty: {target}")
        created: bool = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        client: "BearpondClient" = cls(target)
        client._save_config(WorkspaceConfig(server=server_url, repo=repo))
        try:
            with server_client.ServerClient(server_url, repo, token=token) as server:
                # check existence explicitly: an empty repo and a nonexistent repo both 404 the manifest
                if repo not in server.list_repositories():
                    raise server_client.BearpondError(f"no such repository on {server_url}: {repo}")
                client.pull(server)
        except Exception:
            # don't leave a half-born workspace behind — but only ever remove a directory we created
            if created:
                shutil.rmtree(target)
            raise
        return client

    # ------------------------------------------------------------------------------------------------------------------
    @classmethod
    def discover(cls, start: Path) -> "BearpondClient":
        # walks up from start looking for a .bearpond directory — the workspace root is wherever it lives
        current: Path = start.resolve()
        result: Optional["BearpondClient"] = None
        while result is None:
            if (current / STATE_DIR_NAME).is_dir():
                result = cls(current)
            elif current.parent == current:
                raise server_client.BearpondError(f"not a bearpond workspace (no {STATE_DIR_NAME} found above {start})")
            else:
                current = current.parent
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, workdir: Path) -> None:
        self.workdir: Path = workdir

    # ------------------------------------------------------------------------------------------------------------------
    def connect(self, token: str | None = None) -> server_client.ServerClient:
        # a ServerClient bound to the remote this workspace was cloned from
        config: WorkspaceConfig = self._load_config()
        return server_client.ServerClient(config.server, config.repo, token=token)

    # --- workspace state files ----------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def _load_config(self) -> WorkspaceConfig:
        config_path: Path = self.workdir / STATE_DIR_NAME / CONFIG_NAME
        if not config_path.exists():
            raise server_client.BearpondError(
                f"workspace has no remote configured ({config_path} is missing — was it created with `bearpond clone`?)"
            )
        return WorkspaceConfig.model_validate_json(config_path.read_text())

    # ------------------------------------------------------------------------------------------------------------------
    def _save_config(self, config: WorkspaceConfig) -> None:
        self._write_state_file(CONFIG_NAME, config.model_dump_json(indent=2))
    
    # ------------------------------------------------------------------------------------------------------------------
    def _write_state_file(self, name: str, content: str) -> None:
        # write to a temp file then rename over the real one, so a reader never observes a half-written state file
        state_dir: Path = self.workdir / STATE_DIR_NAME
        state_dir.mkdir(parents=True, exist_ok=True)
        path: Path = state_dir / name
        tmp_path: Path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.replace(tmp_path, path)
    
    # ------------------------------------------------------------------------------------------------------------------
    def tracked_manifest(self) -> types.Manifest | None:
        # the pristine copy of the server manifest this workspace claims to represent — None if never pulled
        manifest: types.Manifest | None = None
        manifest_path: Path = self.workdir / STATE_DIR_NAME / MANIFEST_NAME
        if manifest_path.exists():
            manifest = types.Manifest.model_validate_json(manifest_path.read_text())
        return manifest
    
    # ------------------------------------------------------------------------------------------------------------------
    def _save_manifest(self, manifest: types.Manifest) -> None:
        self._write_state_file(MANIFEST_NAME, manifest.model_dump_json(indent=2))
    
    # ------------------------------------------------------------------------------------------------------------------
    def _load_pending(self) -> dict[str, FileState]:
        # progress of an interrupted pull — the file only exists when a pull died partway through
        pending: dict[str, FileState] = {}
        pending_path: Path = self.workdir / STATE_DIR_NAME / PENDING_NAME
        if pending_path.exists():
            raw: dict[str, list] = json.loads(pending_path.read_text())
            pending = {path: FileState(size=entry[0], sha256=entry[1]) for path, entry in raw.items()}
        return pending
    
    # ------------------------------------------------------------------------------------------------------------------
    def _save_pending(self, pending: dict[str, FileState]) -> None:
        raw: dict = {path: [entry.size, entry.sha256] for path, entry in pending.items()}
        self._write_state_file(PENDING_NAME, json.dumps(raw, indent=2))
    
    # ------------------------------------------------------------------------------------------------------------------
    def _tracked_files(self) -> dict[str, FileState]:
        # the files the workspace tracks per its manifest — no pending overlay: an interrupted pull's progress is
        # transient download state, not tracked state, and only pull() merges it in (explicitly, for resume)
        manifest: types.Manifest | None = self.tracked_manifest()
        files: dict[str, FileState] = (
            {f.path: FileState(size=f.size, sha256=f.sha256) for f in manifest.files} if manifest else {}
        )
        return files
    
    # ------------------------------------------------------------------------------------------------------------------
    def _staged_manifest(self) -> types.TransactionManifest:
        # the client-side staging area (like git's index): what add() recorded, waiting for commit()
        staged_path: Path = self.workdir / STATE_DIR_NAME / STAGED_NAME
        manifest: types.TransactionManifest = types.TransactionManifest()
        if staged_path.exists():
            manifest = types.TransactionManifest.model_validate_json(staged_path.read_text())
        return manifest
    
    # ------------------------------------------------------------------------------------------------------------------
    def _save_staged(self, manifest: types.TransactionManifest) -> None:
        self._write_state_file(STAGED_NAME, manifest.model_dump_json(indent=2))
    
    # --- read-only views ----------------------------------------------------------------------------------------------
    
    # ------------------------------------------------------------------------------------------------------------------
    def _require_no_pending_pull(self) -> None:
        # a dirty workspace — one with an interrupted pull — only supports pull (to finish it) and status (to see
        # it). Staging operations against an ambiguous base are refused, the same way git refuses to let you work
        # on top of an unfinished merge without resolving or aborting it first.
        pending: dict[str, FileState] = self._load_pending()
        if pending:
            raise server_client.BearpondError(
                f"workspace has an interrupted pull ({len(pending)} file(s) pending) — "
                f"run `bearpond pull` to finish it, or delete {PENDING_NAME} to abandon it"
            )
    
    # ------------------------------------------------------------------------------------------------------------------
    def status(self) -> WorkspaceStatus:
        manifest_for_seq: types.Manifest | None = self.tracked_manifest()
        seq: int = manifest_for_seq.seq if manifest_for_seq is not None else 0
        tracked_dict: dict[str, FileState] = self._tracked_files()
        staged_manifest: types.TransactionManifest = self._staged_manifest()
        staged_paths: set[str] = {f.path for f in staged_manifest.added} | {f.path for f in staged_manifest.removed}
        # files reported via their own status fields — staged via staged_added/staged_removed, pending via pending
        accounted_paths: set[str] = staged_paths | set(self._load_pending())
        
        untracked_paths: list[str] = []
        modified_paths: list[str] = []
        missing_paths: list[str] = []
        if self.workdir.exists():
            for path in sorted(self.workdir.rglob("*.parquet")):
                rel_path: str = path.relative_to(self.workdir).as_posix()
                
                # check if the path is part of the tracked files
                if rel_path in tracked_dict:
                    # drift from what the workspace has recorded: size short-circuits, hash only when the size still matches
                    tracked_file_state: FileState = tracked_dict[rel_path]
                    if path.stat().st_size != tracked_file_state.size:
                        modified_paths.append(rel_path)
                    else:
                        # the size matches, check the hash
                        sha256, _ = server_client.hash_and_size(path)
                        if sha256 != tracked_file_state.sha256:
                            modified_paths.append(rel_path)
                
                # if it's not tracked, is it staged or pending?
                elif rel_path not in accounted_paths:
                    untracked_paths.append(rel_path)
        
        # build the missing_paths list
        for rel_path in tracked_dict:
            # if the file does not exist
            if not (self.workdir / rel_path).is_file():
                # and the path is not in staged.removed or pending
                if rel_path not in accounted_paths:
                    # then it's missing
                    missing_paths.append(rel_path)
        
        return WorkspaceStatus(
            workspace_seq=seq,
            pending=sorted(self._load_pending()),
            staged_added=staged_manifest.added,
            staged_removed=staged_manifest.removed,
            untracked=untracked_paths,
            modified=modified_paths,
            missing=missing_paths,
        )
    
    # --- staging ------------------------------------------------------------------------------------------------------
    
    # ------------------------------------------------------------------------------------------------------------------
    def add(self, targets: list[Path]) -> list[types.FileMetadata]:
        # stages each target for the next commit: directories expand to the .parquet files beneath them, and a
        # file's lake path is its location relative to the workspace root. Re-adding a path updates its entry.
        
        # make sure we don't have a hanging interrupted pull
        self._require_no_pending_pull()
        
        # create the list of resolved version of each target path; target dirs are expanded
        resolved_paths: list[Path] = []
        for target in targets:
            # resolve up front so the relative_to() check below works whether targets are relative or absolute
            target = target.resolve()
            if target.is_dir():
                # for a directory, include all child parquet files recursively
                resolved_paths.extend(sorted(target.rglob("*.parquet")))
            elif target.is_file():
                # if it's a file, just add it
                resolved_paths.append(target)
            else:
                raise server_client.BearpondError(f"no such file or directory: {target}")
        
        # get the staged manifest; we at least get an empty one
        staged_manifest: types.TransactionManifest = self._staged_manifest()
        # we use a dict here to handle upserts, essentially multiple inserts of the same file should be OK
        staged_added_dict: dict[str, types.FileMetadata] = {f.path: f for f in staged_manifest.added}
        
        # loop through all the resolved paths
        result_list: list[types.FileMetadata] = []
        for path in resolved_paths:
            # try to get the relative path
            try:
                rel_path: str = path.relative_to(self.workdir.resolve()).as_posix()
            except ValueError:
                raise server_client.BearpondError(f"path is outside the working directory: {path}")
            
            # validate the hive layout and parquet file name
            try:
                types.validate_hive_parquet_path(rel_path)
            except ValueError as e:
                raise server_client.BearpondError(str(e))
            
            # get the hash and size and create the FileMetadata entry
            sha256, size = server_client.hash_and_size(path)
            entry: types.FileMetadata = types.FileMetadata(path=rel_path, size=size, sha256=sha256)
            
            # upsert the FileMetadata entry
            staged_added_dict[rel_path] = entry
            
            # add to the result_list
            result_list.append(entry)
        
        # update the staged manifest with the new list of added paths
        self._save_staged(
            types.TransactionManifest(added=list(staged_added_dict.values()), removed=staged_manifest.removed))
        return result_list
    
    # ------------------------------------------------------------------------------------------------------------------
    def rm(self, targets: list[Path]) -> types.TransactionManifest:
        # stages each target for removal in the next commit. The removal entry carries the workspace's recorded metadata — the
        # exact content the workspace last observed — which is what the server's safe-removal check compares
        # against, so rm works offline and regardless of the file's current state on disk. Removing a path that
        # is staged for addition but not yet committed simply unstage it: there is nothing in the lake to remove.

        # make sure we don't have a hanging interrupted pull
        self._require_no_pending_pull()
        
        tracked_dict: dict[str, FileState] = self._tracked_files()
        staged_manifest: types.TransactionManifest = self._staged_manifest()
        staged_added_dict: dict[str, types.FileMetadata] = {f.path: f for f in staged_manifest.added}
        staged_removed_dict: dict[str, types.FileMetadata] = {f.path: f for f in staged_manifest.removed}
        
        # loop over all the specified targets
        for target in targets:
            # resolve and get relative path
            target = target.resolve()
            try:
                rel_path: str = target.relative_to(self.workdir.resolve()).as_posix()
            except ValueError:
                raise server_client.BearpondError(f"path is outside the working directory: {target}")
            
            
            if rel_path in tracked_dict:
                # a tracked path: stage the removal against the recorded content, cancelling any pending re-add
                staged_added_dict.pop(rel_path, None)
                observed: FileState = tracked_dict[rel_path]
                staged_removed_dict[rel_path] = types.FileMetadata(
                    path=rel_path, size=observed.size, sha256=observed.sha256
                )
            else:
                # nothing in the lake to remove — rm can only mean "cancel a pending add"
                if rel_path in staged_added_dict:
                    del staged_added_dict[rel_path]
                else:
                    raise server_client.BearpondError(f"not tracked by the workspace: {rel_path}")
        
        # update the staged manifest with the new list of added paths
        result: types.TransactionManifest = types.TransactionManifest(
            added=list(staged_added_dict.values()), removed=list(staged_removed_dict.values())
        )
        self._save_staged(result)
        return result
    
    # --- server-driven workflows --------------------------------------------------------------------------------------
    
    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, server: server_client.ServerClient, message: str | None = None) -> types.CommitResponse:
        # the one short-lived server interaction: begin, upload everything staged, commit — all in one go
        self._require_no_pending_pull()
        manifest: types.TransactionManifest = self._staged_manifest()
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
        
        (self.workdir / STATE_DIR_NAME / STAGED_NAME).unlink(missing_ok=True)
        for f in manifest.removed:
            doomed: Path = self.workdir / f.path
            doomed.unlink(missing_ok=True)
            prune_empty_dirs(self.workdir, doomed)
        self._save_manifest(new_manifest)
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def pull(self, server: server_client.ServerClient, dry_run: bool = False) -> None:
        # let's make sure that the workdir exists
        self.workdir.mkdir(parents=True, exist_ok=True)
        
        # get the manifest from the server
        manifest: types.Manifest = server.get_manifest()
        manifest_files: dict[str, FileState] = {
            f.path: FileState(size=f.size, sha256=f.sha256) for f in manifest.files
        }
        
        # get the workspace manifes and the workspace files
        # the workspace files are the ones from the current manifest as well as from an interrupted pull
        workspace_manifest: types.Manifest | None = self.tracked_manifest()
        # the resume view: tracked files plus whatever an interrupted pull already fetched — the one place
        # pending merges into the file set, and it's explicit
        workspace_files: dict[str, FileState] = self._tracked_files()
        workspace_files.update(self._load_pending())
        
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
            # progress is saved after every single file so a crash mid-pull loses at most the file in flight: the
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
            
            # delete files, pruning each deleted file's emptied ancestor directories as we go
            for rel_path in to_delete:
                dest_path = self.workdir / rel_path
                dest_path.unlink(missing_ok=True)
                prune_empty_dirs(self.workdir, dest_path)
                print(f"  pruned: {rel_path}")
            
            # we've updated to the new manifest, so let's make it official
            self._save_manifest(manifest)
            (self.workdir / STATE_DIR_NAME / PENDING_NAME).unlink(missing_ok=True)
            print(
                f"Pulled to manifest-{manifest.seq:08d}: "
                f"+{len(to_download)} -{len(to_delete)} (total {len(manifest.files)} files)"
            )
