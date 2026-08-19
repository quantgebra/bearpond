from fastapi import APIRouter, Depends, Request

from ... import types
from .. import repository
from .. import transaction
from . import dependencies

router: APIRouter = APIRouter(prefix="/transactions", tags=["transactions"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.post("")
def begin_transaction(manifest: types.TransactionManifest) -> types.BeginTransactionResponse:
    txn: transaction.Transaction = repository.get_repository().begin_transaction(manifest)
    return types.BeginTransactionResponse(
        txn_id=txn.txn_id,
        added_files=list(txn.load_declared().keys()),
        removed_files=list(txn.load_removed().keys()),
    )


# ----------------------------------------------------------------------------------------------------------------------
@router.put("/{txn_id}/files/{rel_path:path}")
async def upload_file(txn_id: str, rel_path: str, request: Request) -> types.FileMeta:
    txn: transaction.Transaction = repository.get_repository().get_transaction(txn_id)
    return await txn.upload_file(rel_path, request.stream())


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/{txn_id}")
def get_transaction_status(txn_id: str) -> types.TransactionStatus:
    repo: repository.Repository = repository.get_repository()
    result: types.TransactionStatus
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_id)
        result = types.TransactionStatus(txn_id=txn.txn_id, status="open", seq=None)
    except transaction.TransactionNotFoundError:
        # no staging dir — the transaction either never existed (or was aborted), or already committed; the
        # commit record tells the two apart
        record: repository.CommitRecord | None = repo.load_commit_record(txn_id)
        if record is None:
            raise
        result = types.TransactionStatus(txn_id=record.txn_id, status="committed", seq=record.seq)
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.post("/{txn_id}/commit")
def commit_transaction(txn_id: str) -> types.CommitResponse:
    repo: repository.Repository = repository.get_repository()
    result: types.CommitResponse
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_id)
        result = txn.commit()
    except transaction.TransactionNotFoundError:
        # no staging dir, but a commit record exists — this is a retry of a commit that already finished (e.g. its
        # response was lost to a crash or timeout), so answer from the record instead of failing
        record: repository.CommitRecord | None = repo.load_commit_record(txn_id)
        if record is None:
            raise
        result = types.CommitResponse(
            seq=record.seq,
            files_added_count=len(record.new_files),
            files_removed_count=len(record.removed_files),
        )
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.delete("/{txn_id}", status_code=204)
def abort_transaction(txn_id: str) -> None:
    txn: transaction.Transaction = repository.get_repository().get_transaction(txn_id)
    txn.abort()
