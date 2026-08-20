from fastapi import APIRouter, Depends, Request

from ... import types
from .. import metadata_store
from .. import repository
from .. import transaction
from . import dependencies

router: APIRouter = APIRouter(prefix="/transactions", tags=["transactions"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.post("")
def begin_transaction(manifest: types.TransactionManifest) -> types.BeginTransactionResponse:
    txn: transaction.Transaction = repository.get_repository().begin_transaction(manifest)
    return types.BeginTransactionResponse(
        txn_uuid=txn.txn_uuid,
        added_files=list(txn.load_declared().keys()),
        removed_files=list(txn.load_removed().keys()),
    )


# ----------------------------------------------------------------------------------------------------------------------
@router.put("/{txn_uuid}/files/{rel_path:path}")
async def upload_file(txn_uuid: str, rel_path: str, request: Request) -> types.FileMetadata:
    txn: transaction.Transaction = repository.get_repository().get_transaction(txn_uuid)
    return await txn.upload_file(rel_path, request.stream())


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/{txn_uuid}")
def get_transaction_status(txn_uuid: str) -> types.TransactionStatus:
    repo: repository.Repository = repository.get_repository()
    result: types.TransactionStatus
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_uuid)
        result = types.TransactionStatus(txn_uuid=txn.txn_uuid, status="open", seq=None)
    except transaction.TransactionNotFoundError:
        # no staging dir — the transaction either never existed (or was aborted), or already committed; the
        # commit record tells the two apart
        record: metadata_store.CommitRecord | None = repo.metadata_store.get_commit_record(txn_uuid)
        if record is None:
            raise
        result = types.TransactionStatus(txn_uuid=record.txn_uuid, status="committed", seq=record.seq)
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.post("/{txn_uuid}/commit")
def commit_transaction(txn_uuid: str, payload: types.CommitRequest | None = None) -> types.CommitResponse:
    repo: repository.Repository = repository.get_repository()
    result: types.CommitResponse
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_uuid)
        result = txn.commit(reason=payload.reason if payload is not None else None)
    except transaction.TransactionNotFoundError:
        # no staging dir, but a commit record exists — this is a retry of a commit that already finished (e.g. its
        # response was lost to a crash or timeout), so answer from the record instead of failing
        record: metadata_store.CommitRecord | None = repo.metadata_store.get_commit_record(txn_uuid)
        if record is None:
            raise
        result = types.CommitResponse(
            seq=record.seq,
            files_added_count=len(record.added),
            files_removed_count=len(record.removed),
        )
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.delete("/{txn_uuid}", status_code=204)
def abort_transaction(txn_uuid: str) -> None:
    txn: transaction.Transaction = repository.get_repository().get_transaction(txn_uuid)
    txn.abort()
