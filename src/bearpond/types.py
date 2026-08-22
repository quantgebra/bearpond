from typing import Literal

from pydantic import BaseModel

# the wire protocol shared by bearpond's server and client — nothing here depends on either, so server and client
# both depend on this instead of on each other, staying independently deployable while sharing one definition of
# the shapes they exchange, instead of two hand-kept-in-sync copies


# ======================================================================================================================
class FileMetadata(BaseModel):
    path: str
    size: int
    sha256: str


# ======================================================================================================================
# an update replaces the content at an existing path. Both the old and new metadata are recorded so the commit is
# self-describing and the server can verify the update against the exact content the client observed.
class UpdatedFileMetadata(BaseModel):
    path: str
    old_size: int
    old_sha256: str
    new_size: int
    new_sha256: str


# ======================================================================================================================
class TransactionManifest(BaseModel):
    added: list[FileMetadata] = []
    removed: list[FileMetadata] = []
    updated: list[UpdatedFileMetadata] = []


# ======================================================================================================================
class BeginTransactionResponse(BaseModel):
    txn_uuid: str
    added_files: list[str]
    removed_files: list[str]
    updated_files: list[str]


# ======================================================================================================================
class CommitRequest(BaseModel):
    reason: str | None = None  # the commit message — the "why" recorded on the CommitRecord
    # user arrives with multi-user auth; for now the server records commits with user=None


# ======================================================================================================================
class CommitResponse(BaseModel):
    seq: int
    files_added_count: int
    files_removed_count: int
    files_updated_count: int


# ======================================================================================================================
class TransactionStatus(BaseModel):
    txn_uuid: str
    status: Literal["open", "committed"]
    seq: int | None  # the manifest version the commit produced — set only once status is "committed"


# ======================================================================================================================
class Manifest(BaseModel):
    seq: int  # monotonically increasing version number for this snapshot of the lake, incremented on every change
    created_at: str | None  # None only for the zero-files, never-committed-to state a fresh lake starts in
    files: list[FileMetadata]


# ======================================================================================================================
# one record per commit, kept forever — the full history of how the lake reached its current state. The full
# FileMetadata of every added/removed path and UpdatedFileMetadata of every updated path is included (not just the
# paths) so a metadata store can be rebuilt from these records alone if the database is ever lost or corrupted.
#
# commit_hash is the record's content address, git-style: a hash over the change, its position in history
# (parent_commit_hash + seq), when, by whom, and why — making every record self-certifying and the chain of
# records tamper-evident. txn_uuid is NOT part of the hash: it's the ephemeral handle of the transaction attempt
# the commit was made through, not part of what the commit certifies.
class CommitRecord(BaseModel):
    commit_hash: str
    parent_commit_hash: str | None
    txn_uuid: str
    seq: int
    committed_at: str
    added: list[FileMetadata]
    removed: list[FileMetadata]
    updated: list[UpdatedFileMetadata]
    user: str | None
    reason: str | None


# ======================================================================================================================
# one page of commit history, newest first. next_cursor is the seq to pass as before_seq for the next page —
# set only when the page is truncated.
class CommitHistoryPage(BaseModel):
    records: list[CommitRecord]
    next_cursor: int | None


# ======================================================================================================================
class CreateRepositoryRequest(BaseModel):
    name: str


# ======================================================================================================================
class RepositoryInfo(BaseModel):
    name: str
