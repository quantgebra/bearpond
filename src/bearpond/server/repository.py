import uuid
from pathlib import Path

from .. import types
from . import metadata_store
from . import object_store
from . import transaction


# ======================================================================================================================
class RepositoryError(Exception):
    pass


# ======================================================================================================================
class Repository:

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        name: str,
        repo_dir: Path,
        store: metadata_store.MetadataStore,
        obj_store: object_store.ObjectStore,
    ) -> None:
        # a repository is a namespace in the metadata store. content-addressed objects live in a server-wide store
        # shared across repositories, so identical bytes are stored once regardless of repo.
        self.name: str = name
        self.metadata_store: metadata_store.MetadataStore = store
        self.object_store: object_store.ObjectStore = obj_store

    # ------------------------------------------------------------------------------------------------------------------
    def record_commit(
        self,
        txn_uuid: str,
        declared: dict[str, transaction.FileMeta],
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
                added=[types.FileMetadata(path=p, size=m.size, sha256=m.sha256) for p, m in declared.items()],
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
            uuid.uuid4().hex, self, declared=added, removed=removed, updated=updated
        )
        self.metadata_store.create_transaction(txn.txn_uuid, added, removed, updated)
        return txn

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_uuid: str) -> transaction.Transaction:
        state: tuple[
            dict[str, transaction.FileMeta],
            dict[str, transaction.FileMeta],
            dict[str, types.UpdatedFileMetadata],
            dict[str, transaction.FileMeta],
        ] | None = self.metadata_store.get_transaction(txn_uuid)
        if state is None:
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
        declared, removed, updated, uploaded = state
        return transaction.Transaction(txn_uuid, self, declared, removed, updated, uploaded)


# ======================================================================================================================
class RepositoryNotFoundError(RepositoryError):
    pass


# ======================================================================================================================
class RepositoryExistsError(RepositoryError):
    pass


# ======================================================================================================================
class InvalidRepositoryNameError(RepositoryError):
    pass
