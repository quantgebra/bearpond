from dataclasses import dataclass, field

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
# a pure in-memory representation of an in-flight transaction. persistence is handled by the metadata store, so the
# server remains stateless and transactions survive process restarts. behavior that needs the object or metadata
# store (upload, commit, abort) lives on Repository, which owns those collaborators.
#
# added/removed/updated/uploaded are lists sorted by path — the same shape as the wire TransactionManifest and the
# persisted CommitRecord, so no dict↔list conversion is needed anywhere along the lifecycle
@dataclass
class Transaction:
    txn_uuid: types.TxnUuid
    # when the transaction was begun, stamped by the server (ISO 8601) — client clocks are never trusted
    created_at: str
    added: list[types.FileMetadata]
    removed: list[types.FileMetadata]
    updated: list[types.UpdatedFileMetadata]
    uploaded: list[types.FileMetadata] = field(default_factory=list)
    # who opened the transaction and why — declared at begin time and recorded on the CommitRecord at commit
    user: str | None = None
    reason: str | None = None
