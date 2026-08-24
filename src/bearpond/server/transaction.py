from dataclasses import dataclass, field

from pydantic import BaseModel

from .. import types


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
# a pure in-memory representation of an in-flight transaction. persistence is handled by the metadata store, so the
# server remains stateless and transactions survive process restarts. behavior that needs the object or metadata
# store (upload, commit, abort) lives on Repository, which owns those collaborators.
@dataclass
class Transaction:
    txn_uuid: str
    added: dict[str, FileMeta]
    removed: dict[str, FileMeta]
    updated: dict[str, types.UpdatedFileMetadata]
    uploaded: dict[str, FileMeta] = field(default_factory=dict)
