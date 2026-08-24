from fastapi import APIRouter, Depends, Request

from ... import types
from .. import metadata_store
from .. import server as server_module
from .. import transaction
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos/{repo_name}/transactions", tags=["transactions"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.post("")
def begin_transaction(
    repo_name: str,
    manifest: types.TransactionManifest,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.BeginTransactionResponse:
    txn: transaction.Transaction = server.get_repository(repo_name).begin_transaction(manifest)
    return types.BeginTransactionResponse(
        txn_uuid=txn.txn_uuid,
        added_files=list(txn.declared.keys()),
        removed_files=list(txn.removed.keys()),
        updated_files=list(txn.updated.keys()),
    )


# ----------------------------------------------------------------------------------------------------------------------
@router.put("/{txn_uuid}/files/{rel_path:path}")
async def upload_file(
    repo_name: str,
    txn_uuid: str,
    rel_path: str,
    request: Request,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.FileMetadata:
    txn: transaction.Transaction = server.get_repository(repo_name).get_transaction(txn_uuid)
    return await txn.upload_file(rel_path, request.stream())


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/{txn_uuid}")
def get_transaction_status(
    repo_name: str,
    txn_uuid: str,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.TransactionStatus:
    repo = server.get_repository(repo_name)
    result: types.TransactionStatus
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_uuid)
        result = types.TransactionStatus(txn_uuid=txn.txn_uuid, status="open", seq=None)
    except transaction.TransactionNotFoundError:
        # no transaction state in the store — the transaction either never existed (or was aborted), or already
        # committed; the commit record tells the two apart
        record: types.CommitRecord | None = repo.metadata_store.get_commit_record(txn_uuid)
        if record is None:
            raise
        result = types.TransactionStatus(txn_uuid=record.txn_uuid, status="committed", seq=record.seq)
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.post("/{txn_uuid}/commit")
def commit_transaction(
    repo_name: str,
    txn_uuid: str,
    payload: types.CommitRequest | None = None,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.CommitResponse:
    repo = server.get_repository(repo_name)
    result: types.CommitResponse
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_uuid)
        result = txn.commit(reason=payload.reason if payload is not None else None)
    except transaction.TransactionNotFoundError:
        # no transaction state in the store, but a commit record exists — this is a retry of a commit that already
        # finished (e.g. its response was lost to a crash or timeout), so answer from the record instead of failing
        record: types.CommitRecord | None = repo.metadata_store.get_commit_record(txn_uuid)
        if record is None:
            raise
        result = types.CommitResponse(
            seq=record.seq,
            files_added_count=len(record.added),
            files_removed_count=len(record.removed),
            files_updated_count=len(record.updated),
        )
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.delete("/{txn_uuid}", status_code=204)
def abort_transaction(
    repo_name: str,
    txn_uuid: str,
    server: server_module.Server = Depends(dependencies.get_server),
) -> None:
    txn: transaction.Transaction = server.get_repository(repo_name).get_transaction(txn_uuid)
    txn.abort()
