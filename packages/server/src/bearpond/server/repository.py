import bisect
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import orm

from bearpond.protocol import types
from . import commit_record
from . import object_record
from . import object_store
from . import repo_record
from . import transaction
from . import transaction_record
from . import transaction_uploaded_file_record

# serializes the commits of this process, which is all that serializes them on SQLite; a database that has row locks
# serializes the commits of every process too, by locking the row of the repo
_commit_lock: threading.Lock = threading.Lock()

# a file record carried in a transaction's sorted lists
F = TypeVar("F", types.FileMetadata, types.UpdatedFileMetadata)


# ======================================================================================================================
class RepositoryError(Exception):
    pass


# ======================================================================================================================
class Repository:
    
    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _sorted_unique(files: list[F], role: str) -> list[F]:
        # a transaction's file lists are sorted by path
        ordered: list[F] = sorted(files, key=lambda f: f.path)
        
        # once sorted, any duplicates would be right next to each other
        for prev, cur in zip(ordered, ordered[1:]):
            if prev.path == cur.path:
                raise transaction.TransactionValidationError(f"path {role} more than once: {cur.path}")
        return ordered
    
    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _find_by_path(files: list[F], rel_path: str) -> F | None:
        # the transaction's file lists are sorted by path, so a lookup is a binary search rather than a scan
        index: int = bisect.bisect_left(files, rel_path, key=lambda f: f.path)
        result: F | None = None
        if index < len(files) and files[index].path == rel_path:
            result = files[index]
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _diff_manifests(
        current_files: list[types.FileMetadata], target_files: list[types.FileMetadata]
    ) -> types.TransactionManifest:
        # what a caller would need to add/remove/update to turn current_files into target_files
        current_by_path: dict[str, types.FileMetadata] = {f.path: f for f in current_files}
        target_by_path: dict[str, types.FileMetadata] = {f.path: f for f in target_files}
        
        added: list[types.FileMetadata] = []
        updated: list[types.UpdatedFileMetadata] = []
        for path, target_file in target_by_path.items():
            if path not in current_by_path:
                added.append(target_file)
            elif current_by_path[path].sha256 != target_file.sha256:
                updated_file: types.UpdatedFileMetadata = types.UpdatedFileMetadata(
                    path=path,
                    old_sha256=current_by_path[path].sha256,
                    old_size=current_by_path[path].size,
                    new_sha256=target_file.sha256,
                    new_size=target_file.size,
                )
                updated.append(updated_file)
        
        removed: list[types.FileMetadata] = []
        for path, current_file in current_by_path.items():
            if path not in target_by_path:
                removed.append(current_file)
        
        return types.TransactionManifest(added=added, removed=removed, updated=updated)
    
    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        name: str,
        session: orm.Session,
        obj_store: object_store.ObjectStore,
    ) -> None:
        # a repository is a namespace in the metadata tables, read and written in the session of the request that is
        # being served, which whoever made it commits. content-addressed objects live in a server-wide store shared
        # across repositories, so identical bytes are stored once regardless of repo.
        self.name: str = name
        self.session: orm.Session = session
        self.object_store: object_store.ObjectStore = obj_store
    
    # ------------------------------------------------------------------------------------------------------------------
    def begin_transaction(self, manifest: types.TransactionManifest) -> transaction.Transaction:
        # a transaction that neither adds nor removes nor updates anything isn't meaningful
        if not manifest.added and not manifest.removed and not manifest.updated:
            raise transaction.TransactionValidationError("transaction must add, remove, or update at least one file")
        
        # the transaction's canonical shape is path-sorted lists, matching the manifest and the commit record
        added: list[types.FileMetadata] = self._sorted_unique(manifest.added, "added")
        removed: list[types.FileMetadata] = self._sorted_unique(manifest.removed, "removed")
        updated: list[types.UpdatedFileMetadata] = self._sorted_unique(manifest.updated, "updated")
        
        # perform preliminary check to determine whether we should reject the Transaction right now
        # a path can only play one role in a single transaction
        added_paths: set[str] = {f.path for f in added}
        removed_paths: set[str] = {f.path for f in removed}
        updated_paths: set[str] = {f.path for f in updated}
        for overlap_path in added_paths & removed_paths:
            raise transaction.TransactionValidationError(
                f"path cannot be both added and removed: {overlap_path}"
            )
        for overlap_path in added_paths & updated_paths:
            raise transaction.TransactionValidationError(
                f"path cannot be both added and updated: {overlap_path}"
            )
        for overlap_path in removed_paths & updated_paths:
            raise transaction.TransactionValidationError(
                f"path cannot be both removed and updated: {overlap_path}"
            )
        
        # verify removed paths
        for removed_file in removed:
            current_for_removal: types.FileMetadata | None = object_record.ObjectRecord.get_file_by_path(self.session, self.name, removed_file.path)
            # removed path must correspond to an existing path
            if current_for_removal is None:
                raise transaction.TransactionValidationError(
                    f"cannot remove a path that doesn't exist in the lake: {removed_file.path}"
                )
            # the hash of existing path must match hash specified to be removed
            if current_for_removal.sha256 != removed_file.sha256:
                raise transaction.TransactionConflictError(
                    f"{removed_file.path} has changed since it was declared for removal — remove is only safe against the "
                    f"exact content that was observed"
                )
        
        # verify added paths
        for added_file in added:
            existing: types.FileMetadata | None = object_record.ObjectRecord.get_file_by_path(self.session, self.name, added_file.path)
            # added path must not already exist in repository
            if existing is not None:
                if existing.sha256 != added_file.sha256:
                    raise transaction.TransactionConflictError(
                        f"{added_file.path} already exists in the lake with different content — "
                        f"remove it before adding new content"
                    )
                # re-adding identical content would change nothing — a no-op commit is a client bug, not a success
                raise transaction.TransactionConflictError(
                    f"{added_file.path} already exists in the lake with identical content — re-adding it is a no-op"
                )
        
        # verify updated paths
        for updated_file in updated:
            current_for_update: types.FileMetadata | None = object_record.ObjectRecord.get_file_by_path(self.session, self.name, updated_file.path)
            # updated path must correspond to existing file in repository
            if current_for_update is None:
                raise transaction.TransactionValidationError(
                    f"cannot update a path that doesn't exist in the lake: {updated_file.path}"
                )
            # the hash of the existing file must match the expected hash in the transaction
            if current_for_update.sha256 != updated_file.old_sha256:
                raise transaction.TransactionConflictError(
                    f"{updated_file.path} has changed since it was declared for update — update is only safe against the "
                    f"exact content that was observed"
                )
            # the hash of the existing file must NOT match the expected hash in the transaction
            if updated_file.old_sha256 == updated_file.new_sha256:
                raise transaction.TransactionValidationError(
                    f"{updated_file.path} old and new content are identical — updating it is a no-op"
                )
        
        # create the transaction in memory, then persist its state to the metadata store so it survives restarts
        # and can be loaded by any stateless server process. created_at is stamped by the server here — the client
        # never supplies it. user and reason are declared here too, at begin time — they're properties of the
        # transaction's intent, not of the commit call that finalizes it.
        result: transaction.Transaction = transaction.Transaction(
            txn_uuid=types.TxnUuid(uuid.uuid4().hex),
            created_at=datetime.now(timezone.utc).isoformat(),
            added=added,
            removed=removed,
            updated=updated,
            user=manifest.user,
            reason=manifest.reason,
        )
        
        # store the transaction in the MetadataStore
        transaction_record.TransactionRecord.add_transaction(self.session, self.name, result)
        
        # return the newly created Transaction object
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_uuid: types.TxnUuid) -> transaction.Transaction | None:
        # None means no open transaction has this id — the caller decides whether that is an error
        return transaction_record.TransactionRecord.get_transaction(self.session, self.name, txn_uuid)
    
    # ------------------------------------------------------------------------------------------------------------------
    async def upload_file(
        self, txn: transaction.Transaction, rel_path: str, chunks: AsyncIterator[bytes]
    ) -> types.FileMetadata:
        # only files declared as added or updated in the original transaction manifest can be uploaded
        added_hit: types.FileMetadata | None = self._find_by_path(txn.added, rel_path)
        updated_hit: types.UpdatedFileMetadata | None = self._find_by_path(txn.updated, rel_path)
        if added_hit is not None:
            expected_sha256: types.Sha256 = added_hit.sha256
            expected_size: int = added_hit.size
        elif updated_hit is not None:
            expected_sha256 = updated_hit.new_sha256
            expected_size = updated_hit.new_size
        else:
            raise transaction.TransactionValidationError(
                f"path was not declared when the transaction was opened: {rel_path}"
            )
        
        # stream the bytes straight into the content-addressed object store. The store verifies both size and hash,
        # so a mismatch leaves nothing committed at the address.
        try:
            await self.object_store.upload(expected_sha256, chunks, expected_size)
        except object_store.ObjectStoreError as e:
            raise transaction.TransactionValidationError(str(e)) from e
        
        # record the upload so commit() can refuse if anything declared is still missing — one row per file,
        # so each upload is a cheap insert rather than a rewrite of the whole transaction record
        uploaded_file: types.FileMetadata = types.FileMetadata(
            path=rel_path, sha256=expected_sha256, size=expected_size
        )
        self._record_uploaded_file(txn, uploaded_file)
        
        # echo back what was stored so the caller can confirm size and hash match
        return uploaded_file
    
    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, txn: transaction.Transaction) -> types.CommitResponse:
        # refuse to commit while any declared file hasn't actually been uploaded yet
        required_uploads: set[str] = {f.path for f in txn.added} | {f.path for f in txn.updated}
        missing: set[str] = required_uploads - {f.path for f in txn.uploaded}
        if missing:
            raise transaction.TransactionValidationError(f"declared files not yet uploaded: {sorted(missing)}")
        
        with _commit_lock:
            # objects were placed during upload; commit just verifies they are still present before recording
            # the mapping. A missing object means an upload was lost or cleaned up since begin.
            for added_file in txn.added:
                if not self.object_store.contains_address(added_file.sha256):
                    raise transaction.TransactionConflictError(
                        f"uploaded object missing from store: {added_file.path}"
                    )
            for updated_file in txn.updated:
                if not self.object_store.contains_address(updated_file.new_sha256):
                    raise transaction.TransactionConflictError(
                        f"uploaded object missing from store: {updated_file.path}"
                    )
            
            # the commit point: one atomic metadata transaction flips every pointer. Idempotent by txn_uuid,
            # so a client that lost the response can safely retry. user and reason come from the transaction
            # as declared at begin time.
            record: types.CommitRecord = self._record_commit(txn)
            
            # the transaction state has served its purpose
            transaction_record.TransactionRecord.delete_by_txn_uuid(self.session, self.name, txn.txn_uuid)
        
        # report the manifest version this commit produced and how many files were added/removed/updated
        result: types.CommitResponse = types.CommitResponse(
            seq=record.seq,
            files_added_count=len(txn.added),
            files_removed_count=len(txn.removed),
            files_updated_count=len(txn.updated),
        )
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_response(self, txn_uuid: types.TxnUuid) -> types.CommitResponse | None:
        # a transaction's state is deleted once it commits, so a retried commit (e.g. its response was lost to a
        # crash or timeout) is answered from the commit record instead. None means no commit has this txn_uuid
        prior: types.CommitRecord | None = commit_record.CommitRecord.get_commit_by_txn_uuid(
            self.session, self.name, txn_uuid
        )
        result: types.CommitResponse | None = None
        if prior is not None:
            result = types.CommitResponse(
                seq=prior.seq,
                files_added_count=len(prior.added),
                files_removed_count=len(prior.removed),
                files_updated_count=len(prior.updated),
            )
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def abort(self, txn: transaction.Transaction) -> None:
        # TODO: MBZ: 20260826: Perhaps we should remove the added objects? I suppose GC will do that
        # objects uploaded for this transaction are left in the store as orphans — a background GC reclaims any
        # object not referenced by a committed manifest. This keeps abort simple and stateless.
        transaction_record.TransactionRecord.delete_by_txn_uuid(self.session, self.name, txn.txn_uuid)
    
    # ------------------------------------------------------------------------------------------------------------------
    def get_current_seq(self) -> int:
        # the version of the lake now, 0 while nothing has been committed
        return commit_record.CommitRecord.get_current_seq(self.session, self.name)
    
    # ------------------------------------------------------------------------------------------------------------------
    def get_manifest(self, seq: int) -> types.Manifest | None:
        # the lake as of the given seq: everything added at or before it and not yet removed by then. Returns None for
        # a seq with no commit record (0 — the never-committed state — or beyond the current seq). Every recorded commit
        # advances the seq exactly once, so each seq has exactly one commit record, and its time is the manifest's.
        committed_at: str | None = commit_record.CommitRecord.get_committed_at_by_seq(self.session, self.name, seq)
        manifest: types.Manifest | None = None
        if committed_at is not None:
            files: list[types.FileMetadata] = object_record.ObjectRecord.get_files_as_of_seq(
                self.session, self.name, seq
            )
            manifest = types.Manifest(seq=seq, created_at=committed_at, files=files)
        return manifest
    
    # ------------------------------------------------------------------------------------------------------------------
    def revert(self, target_seq: int, user: str | None = None, reason: str | None = None) -> types.CommitResponse:
        # resolve the current and target states; get_manifest returns None for any seq with no real commit record
        current_manifest: types.Manifest | None = self.get_manifest(self.get_current_seq())
        target_manifest: types.Manifest | None = self.get_manifest(target_seq)
        if target_manifest is None:
            raise transaction.TransactionValidationError(f"no such manifest version: {target_seq}")
        
        # what would need to change to make the current state match the target version
        current_files: list[types.FileMetadata] = current_manifest.files if current_manifest is not None else []
        diff: types.TransactionManifest = self._diff_manifests(current_files, target_manifest.files)
        
        # historical content is normally retained forever, but a future GC could reclaim something no longer live
        # at any retained seq — this is the one place that would surface as a revert failure rather than corruption
        for added_file in diff.added:
            if not self.object_store.contains_address(added_file.sha256):
                raise transaction.TransactionValidationError(
                    f"cannot revert: content for {added_file.path} is no longer available"
                )
        for updated_file in diff.updated:
            if not self.object_store.contains_address(updated_file.new_sha256):
                raise transaction.TransactionValidationError(
                    f"cannot revert: content for {updated_file.path} is no longer available"
                )
        
        # drive the standard begin/commit lifecycle exactly as a client would, reusing every existing conflict
        # check for free — but skip the upload step, since the content is already resident (verified above)
        manifest: types.TransactionManifest = types.TransactionManifest(
            added=diff.added, removed=diff.removed, updated=diff.updated, user=user, reason=reason,
        )
        txn: transaction.Transaction = self.begin_transaction(manifest)
        for added_file in txn.added:
            self._record_uploaded_file(txn, added_file)
        for updated_file in txn.updated:
            uploaded_file: types.FileMetadata = types.FileMetadata(
                path=updated_file.path, sha256=updated_file.new_sha256, size=updated_file.new_size
            )
            self._record_uploaded_file(txn, uploaded_file)
        
        return self.commit(txn)
    
    # ------------------------------------------------------------------------------------------------------------------
    def _record_uploaded_file(self, txn: transaction.Transaction, uploaded_file: types.FileMetadata) -> None:
        # the database is the system of record for what has been uploaded
        transaction_uploaded_file_record.TransactionUploadedFileRecord.record_file(
            self.session, self.name, txn.txn_uuid, uploaded_file
        )
        
        # keep the in-memory transaction in step so the caller can commit the same object — the list stays sorted by
        # path with one entry per path, matching what the store returns on load
        index: int = bisect.bisect_left(txn.uploaded, uploaded_file.path, key=lambda f: f.path)
        if index < len(txn.uploaded) and txn.uploaded[index].path == uploaded_file.path:
            txn.uploaded[index] = uploaded_file
        else:
            txn.uploaded.insert(index, uploaded_file)

    
    # ------------------------------------------------------------------------------------------------------------------
    def _record_commit(self, txn: transaction.Transaction) -> types.CommitRecord:
        # the commit point: every pointer flips, and the commit is chained onto the history, in the one transaction of
        # the request. Idempotent by txn_uuid, so a client that lost the response can safely retry.
        # the repo is locked first, so that the checks below and the changes they guard are one atomic unit: a
        # concurrent commit can't slip between check and act
        if repo_record.RepoRecord.get_locked_by_name(self.session, self.name) is None:
            raise RepositoryNotFoundError(f"unknown repository: {self.name}")
        
        # a commit that already recorded itself is a retry after a lost response — answer with the original record
        result: types.CommitRecord | None = commit_record.CommitRecord.get_commit_by_txn_uuid(
            self.session, self.name, txn.txn_uuid
        )
        if result is None:
            self._check_commit_against_lake(txn)
            result = self._write_commit(txn)
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def _check_commit_against_lake(self, txn: transaction.Transaction) -> None:
        # what the lake holds now, which another commit may have changed since the transaction began
        current_path_2_metadata: dict[str, types.FileMetadata] = object_record.ObjectRecord.get_live_files_by_path(
            self.session, self.name
        )
        
        # a path can only be in one of added, removed, or updated within a single commit
        added_paths: set[str] = {f.path for f in txn.added}
        removed_paths: set[str] = {f.path for f in txn.removed}
        updated_paths: set[str] = {f.path for f in txn.updated}
        for path in added_paths & removed_paths:
            raise transaction.TransactionConflictError(f"path cannot be both added and removed: {path}")
        for path in added_paths & updated_paths:
            raise transaction.TransactionConflictError(f"path cannot be both added and updated: {path}")
        for path in removed_paths & updated_paths:
            raise transaction.TransactionConflictError(f"path cannot be both removed and updated: {path}")
        
        # an added path must not be live at all — different content requires a remove first, identical content is a
        # no-op, and both are rejected: every recorded commit changes the lake by construction
        for added_file in txn.added:
            if added_file.path in current_path_2_metadata:
                if current_path_2_metadata[added_file.path].sha256 != added_file.sha256:
                    raise transaction.TransactionConflictError(
                        f"{added_file.path} already exists in the lake with different content — "
                        f"remove it before adding new content"
                    )
                raise transaction.TransactionConflictError(
                    f"{added_file.path} already exists in the lake with identical content — re-adding it is a no-op"
                )
        
        # a removal is only safe against the exact content the client observed when it decided to remove
        for removed_file in txn.removed:
            if removed_file.path not in current_path_2_metadata:
                raise transaction.TransactionConflictError(f"path to remove no longer exists: {removed_file.path}")
            if current_path_2_metadata[removed_file.path].sha256 != removed_file.sha256:
                raise transaction.TransactionConflictError(
                    f"{removed_file.path} has changed since it was declared for removal — "
                    f"remove is only safe against the exact content that was observed"
                )
        
        # an update is only safe against the exact content the client observed, and the new content must actually be
        # different — otherwise the commit would be a no-op
        for updated_file in txn.updated:
            if updated_file.path not in current_path_2_metadata:
                raise transaction.TransactionConflictError(f"path to update no longer exists: {updated_file.path}")
            if current_path_2_metadata[updated_file.path].sha256 != updated_file.old_sha256:
                raise transaction.TransactionConflictError(
                    f"{updated_file.path} has changed since it was declared for update — "
                    f"update is only safe against the exact content that was observed"
                )
            if updated_file.old_sha256 == updated_file.new_sha256:
                raise transaction.TransactionConflictError(
                    f"{updated_file.path} old and new content are identical — updating it is a no-op"
                )
    
    # ------------------------------------------------------------------------------------------------------------------
    def _write_commit(self, txn: transaction.Transaction) -> types.CommitRecord:
        # the checks guarantee this commit changes the mapping, so the seq always advances; the head of the chain
        # becomes this commit's parent, binding it to the full history behind it
        committed_at: str = datetime.now(timezone.utc).isoformat()
        seq: int = commit_record.CommitRecord.get_current_seq(self.session, self.name) + 1
        parent_commit_hash: str | None = commit_record.CommitRecord.get_head_commit_hash(self.session, self.name)
        commit_hash: str = commit_record.CommitRecord.compute_commit_hash(
            parent_commit_hash, seq, committed_at, txn.added, txn.removed, txn.updated, txn.user, txn.reason
        )
        
        # flip the pointers: a removed or updated path's live row is marked as removed, which keeps the whole history
        # of the path, and an added or updated path gets a new live row
        for added_file in txn.added:
            object_record.ObjectRecord.add_live_file(self.session, self.name, added_file, txn.txn_uuid, seq)
        for removed_file in txn.removed:
            object_record.ObjectRecord.remove_live_file(self.session, self.name, removed_file.path, txn.txn_uuid, seq)
        for updated_file in txn.updated:
            object_record.ObjectRecord.remove_live_file(self.session, self.name, updated_file.path, txn.txn_uuid, seq)
            replacement: types.FileMetadata = types.FileMetadata(
                path=updated_file.path, sha256=updated_file.new_sha256, size=updated_file.new_size
            )
            object_record.ObjectRecord.add_live_file(self.session, self.name, replacement, txn.txn_uuid, seq)
        
        result: types.CommitRecord = types.CommitRecord(
            commit_hash=commit_hash,
            parent_commit_hash=parent_commit_hash,
            txn_uuid=txn.txn_uuid,
            seq=seq,
            committed_at=committed_at,
            created_at=txn.created_at,
            added=txn.added,
            removed=txn.removed,
            updated=txn.updated,
            user=txn.user,
            reason=txn.reason,
        )
        try:
            commit_record.CommitRecord.add_commit(self.session, self.name, result)
        except sqlalchemy_exc.IntegrityError as error:
            raise transaction.TransactionConflictError(
                f"another commit landed in the meantime: {error.orig}"
            ) from error
        return result


# ======================================================================================================================
class RepositoryNotFoundError(RepositoryError):
    pass


# ======================================================================================================================
class RepositoryExistsError(RepositoryError):
    pass


# ======================================================================================================================
class InvalidRepositoryNameError(RepositoryError):
    pass
