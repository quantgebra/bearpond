from fastapi import APIRouter, Depends, Request

from ... import types
from .. import repository
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
        added_files=[f.path for f in txn.added],
        removed_files=[f.path for f in txn.removed],
        updated_files=[f.path for f in txn.updated],
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
    repo: repository.Repository = server.get_repository(repo_name)
    return await repo.upload_file(txn_uuid, rel_path, request.stream())


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/{txn_uuid}")
def get_transaction_status(
    repo_name: str,
    txn_uuid: str,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.TransactionStatus:
    repo: repository.Repository = server.get_repository(repo_name)
    result: types.TransactionStatus
    try:
        txn: transaction.Transaction = repo.get_transaction(txn_uuid)
        result = types.TransactionStatus(
            txn_uuid=txn.txn_uuid, status="open", seq=None, created_at=txn.created_at
        )
    except transaction.TransactionNotFoundError:
        # no transaction state in the store — the transaction either never existed (or was aborted), or already
        # committed; the commit record tells the two apart
        record: types.CommitRecord | None = repo.metadata_store.get_commit_record_by_txn_uuid(txn_uuid)
        if record is None:
            raise
        result = types.TransactionStatus(
            txn_uuid=record.txn_uuid, status="committed", seq=record.seq, created_at=record.created_at
        )
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.post("/{txn_uuid}/commit")
def commit_transaction(
    repo_name: str,
    txn_uuid: str,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.CommitResponse:
    repo: repository.Repository = server.get_repository(repo_name)
    # user and reason were declared at begin time; Repository.commit answers a retry from the commit record when
    # the transaction state is already gone
    return repo.commit(txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
@router.delete("/{txn_uuid}", status_code=204)
def abort_transaction(
    repo_name: str,
    txn_uuid: str,
    server: server_module.Server = Depends(dependencies.get_server),
) -> None:
    server.get_repository(repo_name).abort(txn_uuid)
