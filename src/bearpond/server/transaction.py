import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from .. import types
from . import object_store

# the repository module is only needed for the type hint below — a real import would create a cycle, since
# repository.py imports this module to construct Transaction instances
if TYPE_CHECKING:
    from . import repository

# serializes commits so the metadata commit and cleanup never interleave between transactions
_commit_lock: threading.Lock = threading.Lock()


# ======================================================================================================================
class TransactionError(Exception):
    pass


# ======================================================================================================================
class TransactionNotFoundError(TransactionError):
    pass


# ======================================================================================================================
class TransactionValidationError(TransactionError):
    pass


# ======================================================================================================================
class TransactionConflictError(TransactionError):
    pass


# ======================================================================================================================
# size/sha256 for one path, keyed externally by path in the persisted transaction state — distinct from
# types.FileMetadata, which is the wire-protocol shape and carries its own path field
class FileMeta(BaseModel):
    size: int
    sha256: str


# ======================================================================================================================
class Transaction:

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        txn_uuid: str,
        repo: "repository.Repository",
        declared: dict[str, FileMeta],
        removed: dict[str, FileMeta],
        updated: dict[str, types.UpdatedFileMetadata],
        uploaded: dict[str, FileMeta] | None = None,
    ) -> None:
        # a pure in-memory representation of an in-flight transaction. persistence is handled by the metadata store,
        # so the server remains stateless and transactions survive process restarts.
        self.txn_uuid: str = txn_uuid
        self.repo: "repository.Repository" = repo
        self.declared: dict[str, FileMeta] = declared
        self.removed: dict[str, FileMeta] = removed
        self.updated: dict[str, types.UpdatedFileMetadata] = updated
        self.uploaded: dict[str, FileMeta] = uploaded or {}

    # ------------------------------------------------------------------------------------------------------------------
    async def upload_file(self, rel_path: str, chunks: AsyncIterator[bytes]) -> types.FileMetadata:
        # only files declared as added or updated in the original transaction manifest can be uploaded
        if rel_path in self.declared:
            expected_size: int = self.declared[rel_path].size
            expected_sha256: str = self.declared[rel_path].sha256
        elif rel_path in self.updated:
            expected_size = self.updated[rel_path].new_size
            expected_sha256 = self.updated[rel_path].new_sha256
        else:
            raise TransactionValidationError(f"path was not declared when the transaction was opened: {rel_path}")

        # stream the bytes straight into the content-addressed object store. The store verifies both size and hash,
        # so a mismatch leaves nothing committed at the address.
        try:
            await self.repo.object_store.upload(expected_sha256, chunks, expected_size)
        except object_store.ObjectStoreError as e:
            raise TransactionValidationError(str(e)) from e

        # mark this path as uploaded so commit() can refuse if anything declared is still missing
        self.uploaded[rel_path] = FileMeta(size=expected_size, sha256=expected_sha256)
        self.repo.metadata_store.save_transaction_uploaded(self.txn_uuid, self.uploaded)

        # echo back what was stored so the caller can confirm size and hash match
        return types.FileMetadata(path=rel_path, size=expected_size, sha256=expected_sha256)

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, reason: str | None = None) -> types.CommitResponse:
        # refuse to commit while any declared file hasn't actually been uploaded yet
        required_uploads: set[str] = set(self.declared) | set(self.updated)
        missing: set[str] = required_uploads - set(self.uploaded)
        if missing:
            raise TransactionValidationError(f"declared files not yet uploaded: {sorted(missing)}")

        with _commit_lock:
            # objects were placed during upload; commit just verifies they are still present before recording the
            # mapping. A missing object means an upload was lost or cleaned up since begin.
            for rel_path, meta in self.declared.items():
                if not self.repo.object_store.contains_address(meta.sha256):
                    raise TransactionConflictError(f"uploaded object missing from store: {rel_path}")
            for rel_path, meta in self.updated.items():
                if not self.repo.object_store.contains_address(meta.new_sha256):
                    raise TransactionConflictError(f"uploaded object missing from store: {rel_path}")

            # the commit point: one atomic metadata transaction flips every pointer. Idempotent by txn_uuid, so a
            # client that lost the response can safely retry.
            record: types.CommitRecord = self.repo.record_commit(
                self.txn_uuid, self.declared, self.removed, self.updated, reason=reason
            )

            # the transaction state has served its purpose
            self.repo.metadata_store.delete_transaction(self.txn_uuid)

        # report the manifest version this commit produced and how many files were added/removed/updated
        return types.CommitResponse(
            seq=record.seq,
            files_added_count=len(self.declared),
            files_removed_count=len(self.removed),
            files_updated_count=len(self.updated),
        )

    # ------------------------------------------------------------------------------------------------------------------
    def abort(self) -> None:
        # objects uploaded for this transaction are left in the store as orphans — a background GC reclaims any
        # object not referenced by a committed manifest. This keeps abort simple and stateless.
        self.repo.metadata_store.delete_transaction(self.txn_uuid)
