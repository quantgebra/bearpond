import re
from typing import Literal

from pydantic import BaseModel

# the wire protocol shared by bearpond's server and client — nothing here depends on either, so server and client
# both depend on this instead of on each other, staying independently deployable while sharing one definition of
# the shapes they exchange, instead of two hand-kept-in-sync copies

# a hive partition directory segment, e.g. "year=2024" — key=value, no slashes or extra '=' in either side
_HIVE_PARTITION_SEGMENT: re.Pattern = re.compile(r"^\w+=[^/=]+$")


# ----------------------------------------------------------------------------------------------------------------------
def validate_hive_parquet_path(rel_path: str) -> None:
    # the path convention both sides enforce: .parquet files under hive-style key=value partition directories.
    # raises ValueError on any violation — each side maps that to its own error type.
    if not rel_path.endswith(".parquet"):
        raise ValueError(f"path is not a parquet file: {rel_path}")

    # every directory segment before the filename must be a hive partition key=value pair
    segments: list[str] = rel_path.split("/")
    for segment in segments[:-1]:
        if not _HIVE_PARTITION_SEGMENT.match(segment):
            raise ValueError(f"path segment is not a valid hive partition (expected key=value): {segment!r} in {rel_path}")


# ======================================================================================================================
class FileMetadata(BaseModel):
    path: str
    size: int
    sha256: str


# ======================================================================================================================
class TransactionManifest(BaseModel):
    added: list[FileMetadata] = []
    removed: list[FileMetadata] = []


# ======================================================================================================================
class BeginTransactionResponse(BaseModel):
    txn_uuid: str
    added_files: list[str]
    removed_files: list[str]


# ======================================================================================================================
class CommitRequest(BaseModel):
    reason: str | None = None  # the commit message — the "why" recorded on the CommitRecord
    # user arrives with multi-user auth; for now the server records commits with user=None


# ======================================================================================================================
class CommitResponse(BaseModel):
    seq: int
    files_added_count: int
    files_removed_count: int


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
# FileMetadata of every added/removed path is included (not just the paths) so a metadata store can be rebuilt
# from these records alone if the database is ever lost or corrupted.
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
