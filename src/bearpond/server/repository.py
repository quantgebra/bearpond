import threading
import uuid
from collections.abc import AsyncIterator

from .. import types
from . import metadata_store
from . import object_store
from . import transaction

# serializes commits so the metadata commit and cleanup never interleave between transactions
_commit_lock: threading.Lock = threading.Lock()


# ======================================================================================================================
class RepositoryError(Exception):
    pass


# ======================================================================================================================
class Repository:

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        name: str,
        metadata_store: metadata_store.MetadataStore,
        obj_store: object_store.ObjectStore,
    ) -> None:
        # a repository is a namespace in the metadata store. content-addressed objects live in a server-wide store
        # shared across repositories, so identical bytes are stored once regardless of repo.
        self.name: str = name
        self.metadata_store: metadata_store.MetadataStore = metadata_store
        self.object_store: object_store.ObjectStore = obj_store

    # ------------------------------------------------------------------------------------------------------------------
    def record_commit(
        self,
        txn_uuid: str,
        added: dict[str, transaction.FileMeta],
        removed: dict[str, transaction.FileMeta],
        updated: dict[str, types.UpdatedFileMetadata],
        user: str | None = None,
        reason: str | None = None,
    ) -> types.CommitRecord:
        # the commit point: one atomic metadata transaction flips every pointer at once. The metadata store is the
        # source of truth; objects were placed during upload, so this call only records the mapping.
        try:
            record: types.CommitRecord = self.metadata_store.commit_changes(
                txn_uuid,
                added=[types.FileMetadata(path=p, size=m.size, sha256=m.sha256) for p, m in added.items()],
                removed=[types.FileMetadata(path=p, size=m.size, sha256=m.sha256) for p, m in removed.items()],
                updated=list(updated.values()),
                user=user,
                reason=reason,
            )
        except metadata_store.MetadataConflictError as e:
            raise transaction.TransactionConflictError(str(e)) from e
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def begin_transaction(self, manifest: types.TransactionManifest) -> transaction.Transaction:
        # a transaction that neither adds nor removes nor updates anything isn't meaningful
        if not manifest.added and not manifest.removed and not manifest.updated:
            raise transaction.TransactionValidationError("transaction must add, remove, or update at least one file")

        # build the added set, rejecting a path named more than once in the same manifest
        added: dict[str, transaction.FileMeta] = {}
        for added_file in manifest.added:
            if added_file.path in added:
                raise transaction.TransactionValidationError(f"path added more than once: {added_file.path}")
            added[added_file.path] = transaction.FileMeta(size=added_file.size, sha256=added_file.sha256)

        # build the removed set, rejecting a path named more than once in the same manifest
        removed: dict[str, transaction.FileMeta] = {}
        for removed_file in manifest.removed:
            if removed_file.path in removed:
                raise transaction.TransactionValidationError(f"path removed more than once: {removed_file.path}")
            removed[removed_file.path] = transaction.FileMeta(size=removed_file.size, sha256=removed_file.sha256)

        # build the updated set, rejecting a path named more than once in the same manifest
        updated: dict[str, types.UpdatedFileMetadata] = {}
        for updated_file in manifest.updated:
            if updated_file.path in updated:
                raise transaction.TransactionValidationError(f"path updated more than once: {updated_file.path}")
            updated[updated_file.path] = updated_file

        # a path can only play one role in a single transaction
        for overlap_path in set(added) & set(removed):
            raise transaction.TransactionValidationError(
                f"path cannot be both added and removed: {overlap_path}"
            )
        for overlap_path in set(added) & set(updated):
            raise transaction.TransactionValidationError(
                f"path cannot be both added and updated: {overlap_path}"
            )
        for overlap_path in set(removed) & set(updated):
            raise transaction.TransactionValidationError(
                f"path cannot be both removed and updated: {overlap_path}"
            )

        # verify removed paths
        for rel_path, meta in removed.items():
            current: types.FileMetadata | None = self.metadata_store.get_file_metadata(rel_path)
            # removed path must correspond to an existing path
            if current is None:
                raise transaction.TransactionValidationError(
                    f"cannot remove a path that doesn't exist in the lake: {rel_path}"
                )
            # the hash of existing path must match hash specified to be removed
            if current.sha256 != meta.sha256:
                raise transaction.TransactionConflictError(
                    f"{rel_path} has changed since it was declared for removal — remove is only safe against the "
                    f"exact content that was observed"
                )

        # verify added paths
        for rel_path, meta in added.items():
            existing: types.FileMetadata | None = self.metadata_store.get_file_metadata(rel_path)
            # added path must not already exist in repository
            if existing is not None:
                if existing.sha256 != meta.sha256:
                    raise transaction.TransactionConflictError(
                        f"{rel_path} already exists in the lake with different content — "
                        f"remove it before adding new content"
                    )
                # re-adding identical content would change nothing — a no-op commit is a client bug, not a success
                raise transaction.TransactionConflictError(
                    f"{rel_path} already exists in the lake with identical content — re-adding it is a no-op"
                )

        # verify updated paths
        for rel_path, meta in updated.items():
            current: types.FileMetadata | None = self.metadata_store.get_file_metadata(rel_path)
            # updated path must correspond to existing file in repository
            if current is None:
                raise transaction.TransactionValidationError(
                    f"cannot update a path that doesn't exist in the lake: {rel_path}"
                )
            # the hash of the existing file must match the expected hash in the transaction
            if current.sha256 != meta.old_sha256:
                raise transaction.TransactionConflictError(
                    f"{rel_path} has changed since it was declared for update — update is only safe against the "
                    f"exact content that was observed"
                )
            # the hash of the existing file must NOT match the expected hash in the transaction
            if meta.old_sha256 == meta.new_sha256:
                raise transaction.TransactionValidationError(
                    f"{rel_path} old and new content are identical — updating it is a no-op"
                )

        # create the transaction in memory, then persist its state to the metadata store so it survives restarts
        # and can be loaded by any stateless server process
        txn: transaction.Transaction = transaction.Transaction(
            txn_uuid=uuid.uuid4().hex, added=added, removed=removed, updated=updated
        )
        self.metadata_store.store_transaction(txn)
        return txn

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_uuid: str) -> transaction.Transaction:
        txn: transaction.Transaction | None = self.metadata_store.get_transaction(txn_uuid)
        if txn is None:
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
        return txn

    # ------------------------------------------------------------------------------------------------------------------
    async def upload_file(self, txn_uuid: str, rel_path: str, chunks: AsyncIterator[bytes]) -> types.FileMetadata:
        txn: transaction.Transaction = self.get_transaction(txn_uuid)

        # only files declared as added or updated in the original transaction manifest can be uploaded
        if rel_path in txn.added:
            expected_size: int = txn.added[rel_path].size
            expected_sha256: str = txn.added[rel_path].sha256
        elif rel_path in txn.updated:
            expected_size = txn.updated[rel_path].new_size
            expected_sha256 = txn.updated[rel_path].new_sha256
        else:
            raise transaction.TransactionValidationError(f"path was not declared when the transaction was opened: {rel_path}")

        # stream the bytes straight into the content-addressed object store. The store verifies both size and hash,
        # so a mismatch leaves nothing committed at the address.
        try:
            await self.object_store.upload(expected_sha256, chunks, expected_size)
        except object_store.ObjectStoreError as e:
            raise transaction.TransactionValidationError(str(e)) from e

        # record the upload so commit() can refuse if anything declared is still missing — one row per file,
        # so each upload is a cheap insert rather than a rewrite of the whole transaction record
        uploaded_file: types.FileMetadata = types.FileMetadata(path=rel_path, size=expected_size, sha256=expected_sha256)
        self.metadata_store.record_uploaded_file(txn.txn_uuid, uploaded_file)

        # echo back what was stored so the caller can confirm size and hash match
        return uploaded_file

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, txn_uuid: str, reason: str | None = None) -> types.CommitResponse:
        txn: transaction.Transaction | None = self.metadata_store.get_transaction(txn_uuid)
        if txn is None:
            # no transaction state in the store, but a commit record may exist — this is a retry of a commit that
            # already finished (e.g. its response was lost to a crash or timeout), so answer from the record
            prior: types.CommitRecord | None = self.metadata_store.get_commit_record(txn_uuid)
            if prior is None:
                raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
            result: types.CommitResponse = types.CommitResponse(
                seq=prior.seq,
                files_added_count=len(prior.added),
                files_removed_count=len(prior.removed),
                files_updated_count=len(prior.updated),
            )
        else:
            # refuse to commit while any declared file hasn't actually been uploaded yet
            required_uploads: set[str] = set(txn.added) | set(txn.updated)
            missing: set[str] = required_uploads - set(txn.uploaded)
            if missing:
                raise transaction.TransactionValidationError(f"declared files not yet uploaded: {sorted(missing)}")

            with _commit_lock:
                # objects were placed during upload; commit just verifies they are still present before recording
                # the mapping. A missing object means an upload was lost or cleaned up since begin.
                for rel_path, meta in txn.added.items():
                    if not self.object_store.contains_address(meta.sha256):
                        raise transaction.TransactionConflictError(f"uploaded object missing from store: {rel_path}")
                for rel_path, meta in txn.updated.items():
                    if not self.object_store.contains_address(meta.new_sha256):
                        raise transaction.TransactionConflictError(f"uploaded object missing from store: {rel_path}")

                # the commit point: one atomic metadata transaction flips every pointer. Idempotent by txn_uuid,
                # so a client that lost the response can safely retry.
                record: types.CommitRecord = self.record_commit(
                    txn.txn_uuid, txn.added, txn.removed, txn.updated, reason=reason
                )

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
    def abort(self, txn_uuid: str) -> None:
        # refuse to abort an unknown transaction, matching the get-then-abort behavior the API used to drive
        self.get_transaction(txn_uuid)
        # objects uploaded for this transaction are left in the store as orphans — a background GC reclaims any
        # object not referenced by a committed manifest. This keeps abort simple and stateless.
        self.metadata_store.delete_transaction(txn_uuid)


# ======================================================================================================================
class RepositoryNotFoundError(RepositoryError):
    pass


# ======================================================================================================================
class RepositoryExistsError(RepositoryError):
    pass


# ======================================================================================================================
class InvalidRepositoryNameError(RepositoryError):
    pass
