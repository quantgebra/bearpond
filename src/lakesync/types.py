from typing import Literal

from pydantic import BaseModel

# the wire protocol shared by lakesync's server and client — nothing here depends on either, so server and client
# both depend on this instead of on each other, staying independently deployable while sharing one definition of
# the shapes they exchange, instead of two hand-kept-in-sync copies


# ======================================================================================================================
class FileMeta(BaseModel):
    path: str
    size: int
    sha256: str


# ======================================================================================================================
class TransactionManifest(BaseModel):
    added: list[FileMeta] = []
    # size/sha256 are required (not just the path) so the server can verify a file hasn't changed since the client
    # last observed it, before removing it
    removed: list[FileMeta] = []


# ======================================================================================================================
class BeginTransactionResponse(BaseModel):
    txn_id: str
    added_files: list[str]
    removed_files: list[str]


# ======================================================================================================================
class CommitResponse(BaseModel):
    seq: int
    files_added_count: int
    files_removed_count: int


# ======================================================================================================================
class TransactionStatus(BaseModel):
    txn_id: str
    status: Literal["open", "committed"]
    seq: int | None  # the manifest version the commit produced — set only once status is "committed"


# ======================================================================================================================
class Manifest(BaseModel):
    seq: int  # monotonically increasing version number for this snapshot of the lake, incremented on every change
    created_at: str | None  # None only for the zero-files, never-committed-to state a fresh lake starts in
    files: list[FileMeta]
