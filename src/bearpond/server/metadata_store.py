from .. import types
from . import transaction
from .server_metadata_store import DirectoryPage, MetadataConflictError, MetadataStoreError, ServerMetadataStore

__all__ = [
    "DirectoryPage",
    "MetadataConflictError",
    "MetadataStoreError",
    "ServerMetadataStore",
    "MetadataStore",
]


# ======================================================================================================================
# a repo-scoped metadata store: binds a repository name to the server-wide store so callers don't pass the repo on
# every call. this is the interface Repository consumes; the ServerMetadataStore behind it is the pluggable seam
# (SQLite today, Postgres later), while this class just forwards with the repo filled in.
class MetadataStore:

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, repo: str, store: ServerMetadataStore) -> None:
        self._repo: str = repo
        self._store: ServerMetadataStore = store

    # ------------------------------------------------------------------------------------------------------------------
    def get_file_metadata(self, path: str) -> types.FileMetadata | None:
        return self._store.get_file_metadata(self._repo, path)

    # ------------------------------------------------------------------------------------------------------------------
    def list_directory(self, prefix: str, limit: int, cursor: str | None = None) -> DirectoryPage:
        return self._store.list_directory(self._repo, prefix, limit, cursor)

    # ------------------------------------------------------------------------------------------------------------------
    def get_current_seq(self) -> int:
        return self._store.get_current_seq(self._repo)

    # ------------------------------------------------------------------------------------------------------------------
    def get_manifest(self, seq: int) -> types.Manifest | None:
        return self._store.get_manifest(self._repo, seq)

    # ------------------------------------------------------------------------------------------------------------------
    def commit_transaction(self, txn: transaction.Transaction) -> types.CommitRecord:
        return self._store.commit_transaction(self._repo, txn)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_by_txn_uuid(self, txn_uuid: str) -> types.CommitRecord | None:
        return self._store.get_commit_record_by_txn_uuid(self._repo, txn_uuid)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_by_commit_hash(self, commit_hash: str) -> types.CommitRecord | None:
        return self._store.get_commit_record_by_commit_hash(self._repo, commit_hash)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_by_commit_seq(self, seq: int) -> types.CommitRecord | None:
        return self._store.get_commit_record_by_commit_seq(self._repo, seq)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_list(self) -> list[types.CommitRecord]:
        return self._store.get_commit_record_list(self._repo)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_page(self, limit: int, before_seq: int | None = None) -> types.CommitHistoryPage:
        return self._store.get_commit_record_page(self._repo, limit, before_seq)

    # ------------------------------------------------------------------------------------------------------------------
    def store_transaction(self, txn: transaction.Transaction) -> None:
        return self._store.store_transaction(self._repo, txn)

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_uuid: str) -> transaction.Transaction | None:
        return self._store.get_transaction(self._repo, txn_uuid)

    # ------------------------------------------------------------------------------------------------------------------
    def record_uploaded_file(self, txn_uuid: str, file: types.FileMetadata) -> None:
        return self._store.record_uploaded_file(self._repo, txn_uuid, file)

    # ------------------------------------------------------------------------------------------------------------------
    def delete_transaction(self, txn_uuid: str) -> None:
        return self._store.delete_transaction(self._repo, txn_uuid)
