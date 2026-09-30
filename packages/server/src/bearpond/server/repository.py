import bisect
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TypeVar

from .. import types
from . import metadata_store
from . import object_store
from . import repo_metadata_store
from . import transaction

# serializes commits so the metadata commit and cleanup never interleave between transactions
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
        # a transaction's file lists are stored sorted by path
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
            my_metadata_store: repo_metadata_store.RepoMetadataStore,
            obj_store: object_store.ObjectStore,
    ) -> None:
        # a repository is a namespace in the metadata store. content-addressed objects live in a server-wide store
        # shared across repositories, so identical bytes are stored once regardless of repo.
        self.name: str = name
        self.metadata_store: repo_metadata_store.RepoMetadataStore = my_metadata_store
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
            current: types.FileMetadata | None = self.metadata_store.get_file_metadata(removed_file.path)
            # removed path must correspond to an existing path
            if current is None:
                raise transaction.TransactionValidationError(
                    f"cannot remove a path that doesn't exist in the lake: {removed_file.path}"
                )
            # the hash of existing path must match hash specified to be removed
            if current.sha256 != removed_file.sha256:
                raise transaction.TransactionConflictError(
                    f"{removed_file.path} has changed since it was declared for removal — remove is only safe against the "
                    f"exact content that was observed"
                )
        
        # verify added paths
        for added_file in added:
            existing: types.FileMetadata | None = self.metadata_store.get_file_metadata(added_file.path)
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
            current: types.FileMetadata | None = self.metadata_store.get_file_metadata(updated_file.path)
            # updated path must correspond to existing file in repository
            if current is None:
                raise transaction.TransactionValidationError(
                    f"cannot update a path that doesn't exist in the lake: {updated_file.path}"
                )
            # the hash of the existing file must match the expected hash in the transaction
            if current.sha256 != updated_file.old_sha256:
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
        txn: transaction.Transaction = transaction.Transaction(
            txn_uuid=types.TxnUuid(uuid.uuid4().hex),
            created_at=datetime.now(timezone.utc).isoformat(),
            added=added,
            removed=removed,
            updated=updated,
            user=manifest.user,
            reason=manifest.reason,
        )
        
        # store the transaction in the MetadataStore
        self.metadata_store.store_transaction(txn)
        
        # return teh newly created Transaction object
        return txn
    
    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_uuid: types.TxnUuid) -> transaction.Transaction:
        txn: transaction.Transaction | None = self.metadata_store.get_transaction(txn_uuid)
        if txn is None:
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
        return txn
    
    # ------------------------------------------------------------------------------------------------------------------
    async def upload_file(self, txn_uuid: types.TxnUuid, rel_path: str, chunks: AsyncIterator[bytes]) -> types.FileMetadata:
        # get and validate the transaction
        txn: transaction.Transaction = self.get_transaction(txn_uuid)
        
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
                f"path was not declared when the transaction was opened: {rel_path}")
        
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
        self.metadata_store.record_uploaded_file(txn.txn_uuid, uploaded_file)
        
        # echo back what was stored so the caller can confirm size and hash match
        return uploaded_file
    
    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, txn_uuid: types.TxnUuid) -> types.CommitResponse:
        txn: transaction.Transaction | None = self.metadata_store.get_transaction(txn_uuid)
        
        # attempt to find a commit record corresponding to the txn_uuid if we can't find an open Transaction
        if txn is None:
            # no transaction state in the store, but a commit record may exist — this is a retry of a commit that
            # already finished (e.g. its response was lost to a crash or timeout), so answer from the record
            prior: types.CommitRecord | None = self.metadata_store.get_commit_record_by_txn_uuid(txn_uuid)
            if prior is None:
                raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
            result: types.CommitResponse = types.CommitResponse(
                seq=prior.seq,
                files_added_count=len(prior.added),
                files_removed_count=len(prior.removed),
                files_updated_count=len(prior.updated),
            )
            
        # we have a Transaction, so let's try to commit it
        else:
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
                            f"uploaded object missing from store: {added_file.path}")
                for updated_file in txn.updated:
                    if not self.object_store.contains_address(updated_file.new_sha256):
                        raise transaction.TransactionConflictError(
                            f"uploaded object missing from store: {updated_file.path}")
                
                # the commit point: one atomic metadata transaction flips every pointer. Idempotent by txn_uuid,
                # so a client that lost the response can safely retry. user and reason come from the transaction
                # as declared at begin time.
                try:
                    record: types.CommitRecord = self.metadata_store.commit_transaction(txn)
                except metadata_store.MetadataConflictError as e:
                    raise transaction.TransactionConflictError(str(e)) from e
                
                # the transaction state has served its purpose
                self.metadata_store.delete_transaction(txn.txn_uuid)
            
            # report the manifest version this commit produced and how many files were added/removed/updated
            result = types.CommitResponse(
                seq=record.seq,
                files_added_count=len(txn.added),
                files_removed_count=len(txn.removed),
                files_updated_count=len(txn.updated),
            )
        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def abort(self, txn_uuid: types.TxnUuid) -> None:
        # TODO: MBZ: 20260826: Perhaps we should remove the added objects? I suppose GC will do that
        # refuse to abort an unknown transaction, matching the get-then-abort behavior the API used to drive
        self.get_transaction(txn_uuid)
        
        # objects uploaded for this transaction are left in the store as orphans — a background GC reclaims any
        # object not referenced by a committed manifest. This keeps abort simple and stateless.
        self.metadata_store.delete_transaction(txn_uuid)

    # ------------------------------------------------------------------------------------------------------------------
    def revert(self, target_seq: int, user: str | None = None, reason: str | None = None) -> types.CommitResponse:
        # resolve the current and target states; get_manifest returns None for any seq with no real commit record
        current_manifest: types.Manifest | None = self.metadata_store.get_manifest(self.metadata_store.get_current_seq())
        target_manifest: types.Manifest | None = self.metadata_store.get_manifest(target_seq)
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
                    f"cannot revert: content for {added_file.path} is no longer available")
        for updated_file in diff.updated:
            if not self.object_store.contains_address(updated_file.new_sha256):
                raise transaction.TransactionValidationError(
                    f"cannot revert: content for {updated_file.path} is no longer available")

        # drive the standard begin/commit lifecycle exactly as a client would, reusing every existing conflict
        # check for free — but skip the upload step, since the content is already resident (verified above)
        manifest: types.TransactionManifest = types.TransactionManifest(
            added=diff.added, removed=diff.removed, updated=diff.updated, user=user, reason=reason,
        )
        txn: transaction.Transaction = self.begin_transaction(manifest)
        for added_file in txn.added:
            self.metadata_store.record_uploaded_file(txn.txn_uuid, added_file)
        for updated_file in txn.updated:
            uploaded_file: types.FileMetadata = types.FileMetadata(
                path=updated_file.path, sha256=updated_file.new_sha256, size=updated_file.new_size
            )
            self.metadata_store.record_uploaded_file(txn.txn_uuid, uploaded_file)

        return self.commit(txn.txn_uuid)


# ======================================================================================================================
class RepositoryNotFoundError(RepositoryError):
    pass


# ======================================================================================================================
class RepositoryExistsError(RepositoryError):
    pass


# ======================================================================================================================
class InvalidRepositoryNameError(RepositoryError):
    pass
