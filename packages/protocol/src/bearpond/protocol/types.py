from typing import Literal, NewType

from pydantic import BaseModel

# the wire protocol shared by bearpond's server and client — nothing here depends on either, so server and client
# both depend on this instead of on each other, staying independently deployable while sharing one definition of
# the shapes they exchange, instead of two hand-kept-in-sync copies

# a transaction's identifier — a plain str at runtime (zero overhead), but distinct to the type checker so it can't
# be passed where a sha256 or a path is expected, or vice versa
TxnUuid = NewType("TxnUuid", str)

# a file content's hash — distinct from a path, a txn_uuid, or a commit_hash (a different hash over different
# content: the commit's change, not a file's bytes), even though all are hex sha256 strings at runtime
Sha256 = NewType("Sha256", str)

# bumped only on a genuine breaking change to the wire format below (a field removed, renamed, retyped, or made
# required) — pure additions (a new optional field with a default) don't bump it. Client and server each report
# whichever value their own installed bearpond-protocol defines, so a mismatch is detected at connect time.
PROTOCOL_VERSION: int = 1


# ======================================================================================================================
class FileMetadata(BaseModel):
    path: str
    sha256: Sha256
    size: int


# ======================================================================================================================
# an update replaces the content at an existing path. Both the old and new metadata are recorded so the commit is
# self-describing and the server can verify the update against the exact content the client observed.
class UpdatedFileMetadata(BaseModel):
    path: str
    old_sha256: Sha256
    old_size: int
    new_sha256: Sha256
    new_size: int


# ======================================================================================================================
# the full declaration of a transaction's intent, sent at begin time: the files it will touch plus who is making
# the change and why. user and reason are recorded on the CommitRecord when the transaction commits — they belong
# to the transaction as a whole, not to the commit call that finalizes it.
class TransactionManifest(BaseModel):
    added: list[FileMetadata] = []
    removed: list[FileMetadata] = []
    updated: list[UpdatedFileMetadata] = []
    user: str | None = None
    reason: str | None = None
    # when the transaction was begun, stamped by the server (ISO 8601) — client clocks are never trusted.
    # Optional on the wire because the request manifest does not supply it; populated by the server when the
    # manifest is returned or persisted.
    created_at: str | None = None


# ======================================================================================================================
class BeginTransactionResponse(BaseModel):
    txn_uuid: TxnUuid
    added_files: list[str]
    removed_files: list[str]
    updated_files: list[str]


# ======================================================================================================================
class CommitResponse(BaseModel):
    seq: int
    files_added_count: int
    files_removed_count: int
    files_updated_count: int


# ======================================================================================================================
class TransactionStatus(BaseModel):
    txn_uuid: TxnUuid
    status: Literal["open", "committed"]
    seq: int | None  # the manifest version the commit produced — set only once status is "committed"
    created_at: str | None = None  # when the transaction was begun, stamped by the server (ISO 8601)


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
    txn_uuid: TxnUuid
    seq: int
    committed_at: str
    # when the transaction was begun, stamped by the server (ISO 8601). Optional because commits recorded before
    # this field was added do not carry it.
    created_at: str | None = None
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


# ======================================================================================================================
class RevertRequest(BaseModel):
    target_seq: int
    user: str | None = None
    reason: str | None = None


# ======================================================================================================================
class VersionInfo(BaseModel):
    protocol_version: int
    server_version: str
