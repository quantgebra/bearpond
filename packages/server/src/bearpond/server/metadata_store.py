import hashlib
import json
from typing import Protocol

from pydantic import BaseModel

from bearpond.protocol import types
from . import transaction


# ======================================================================================================================
class MetadataStoreError(Exception):
    pass


# ======================================================================================================================
class MetadataConflictError(MetadataStoreError):
    # a commit's declared changes no longer match the current mapping — another commit landed in the meantime
    pass


# ======================================================================================================================
# one page of a directory listing: the files directly under a prefix and the distinct immediate subdirectories
# below it. next_cursor is set when the file page is truncated — pass it back as cursor to get the next page.
class DirectoryPage(BaseModel):
    files: list[types.FileMetadata]
    directories: list[str]
    next_cursor: str | None


# ----------------------------------------------------------------------------------------------------------------------
# every MetadataStore implementation must compute commit_hash identically, so hashes are comparable regardless of
# which backend produced them — this lives here, not in a specific implementation, as part of the shared contract
def compute_commit_hash(
    parent_commit_hash: str | None,
    seq: int,
    committed_at: str,
    added: list[types.FileMetadata],
    removed: list[types.FileMetadata],
    updated: list[types.UpdatedFileMetadata],
    user: str | None,
    reason: str | None,
) -> str:
    # fixed field order and compact separators make the hash reproducible from the exported record alone.
    # added/removed/updated are sorted by path before serializing — they're semantically sets, and the hash must
    # depend only on the change's content, never on list order (same reason git stores tree entries sorted by name)
    payload: dict = {
        "parent_commit_hash": parent_commit_hash,
        "seq": seq,
        "committed_at": committed_at,
        "added": [f.model_dump() for f in sorted(added, key=lambda f: f.path)],
        "removed": [f.model_dump() for f in sorted(removed, key=lambda f: f.path)],
        "updated": [f.model_dump() for f in sorted(updated, key=lambda f: f.path)],
        "user": user,
        "reason": reason,
    }
    canonical: str = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ======================================================================================================================
# server-wide metadata store: every method takes a repo name so one database can host many repositories. this is
# the lake's system of record: which content lives at which path, the full history of how it got there, and the
# manifest sequence. commit_transaction is the commit point of a transaction — implementations must make it atomic
# (all checks and updates in one storage transaction) and idempotent by txn_uuid (a retry after a lost response
# returns the original record).
class MetadataStore(Protocol):

    def get_file_metadata(self, repo: str, path: str) -> types.FileMetadata | None: ...
    def list_directory(self, repo: str, prefix: str, limit: int, cursor: str | None = None) -> DirectoryPage: ...
    def get_current_seq(self, repo: str) -> int: ...
    def get_manifest(self, repo: str, seq: int) -> types.Manifest | None: ...
    def commit_transaction(self, repo: str, txn: transaction.Transaction) -> types.CommitRecord: ...
    def get_commit_record_by_txn_uuid(self, repo: str, txn_uuid: types.TxnUuid) -> types.CommitRecord | None: ...
    def get_commit_record_by_commit_hash(self, repo: str, commit_hash: str) -> types.CommitRecord | None: ...
    def get_commit_record_by_commit_seq(self, repo: str, seq: int) -> types.CommitRecord | None: ...
    def get_commit_record_list(self, repo: str) -> list[types.CommitRecord]: ...
    def get_commit_record_page(self, repo: str, limit: int, before_seq: int | None = None) -> types.CommitHistoryPage: ...
    def register_repo(self, repo: str) -> None: ...
    def list_repos(self) -> list[str]: ...
    def store_transaction(self, repo: str, txn: transaction.Transaction) -> None: ...
    def get_transaction(self, repo: str, txn_uuid: types.TxnUuid) -> transaction.Transaction | None: ...
    def record_uploaded_file(self, repo: str, txn_uuid: types.TxnUuid, file: types.FileMetadata) -> None: ...
    def delete_transaction(self, repo: str, txn_uuid: types.TxnUuid) -> None: ...
    def close(self) -> None: ...
