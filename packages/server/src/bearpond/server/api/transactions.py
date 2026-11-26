from fastapi import APIRouter, Depends, Request
from quantgebra.auth.http import auth_gate

from bearpond.protocol import types
from .. import repository
from .. import commit_record
from .. import transaction
from . import dependencies

router: APIRouter = APIRouter(
    prefix="/repos/{repo_name}/transactions", tags=["transactions"], dependencies=[Depends(dependencies.require_write)]
)


# ----------------------------------------------------------------------------------------------------------------------
@router.post("")
def begin_transaction(
    repo_name: str,
    manifest: types.TransactionManifest,
    found: auth_gate.RequestAuth = Depends(dependencies.require_write),
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.BeginTransactionResponse:
    # a person who is logged in is the author of what they commit, whatever the client says; the shared token has no
    # person behind it, so what the client says is kept
    if found.session is not None:
        manifest = manifest.model_copy(update={"user": found.actor.username})
    txn: transaction.Transaction = repo.begin_transaction(manifest)
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
    txn_uuid: types.TxnUuid,
    rel_path: str,
    request: Request,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.FileMetadata:
    txn: transaction.Transaction = _require_open_transaction(repo, txn_uuid)
    return await repo.upload_file(txn, rel_path, request.stream())


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/{txn_uuid}")
def get_transaction_status(
    repo_name: str,
    txn_uuid: types.TxnUuid,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.TransactionStatus:
    txn: transaction.Transaction | None = repo.get_transaction(txn_uuid)
    result: types.TransactionStatus
    if txn is not None:
        result = types.TransactionStatus(txn_uuid=txn.txn_uuid, status="open", seq=None, created_at=txn.created_at)
    else:
        # no transaction state in the store — the transaction either never existed (or was aborted), or already
        # committed; the commit record tells the two apart
        record: types.CommitRecord | None = commit_record.CommitRecord.get_commit_by_txn_uuid(
            repo.session, repo.name, txn_uuid
        )
        if record is not None:
            result = types.TransactionStatus(
                txn_uuid=record.txn_uuid, status="committed", seq=record.seq, created_at=record.created_at
            )
        else:
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.post("/{txn_uuid}/commit")
def commit_transaction(
    repo_name: str,
    txn_uuid: types.TxnUuid,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.CommitResponse:
    result: types.CommitResponse | None
    txn: transaction.Transaction | None = repo.get_transaction(txn_uuid)
    if txn is not None:
        # user and reason were declared at begin time
        result = repo.commit(txn)
    else:
        # no open transaction — this may be a retry of a commit that already finished, so answer from its record
        result = repo.get_commit_response(txn_uuid)
        if result is None:
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")

    # result can not be None since we raise an Error if it's None
    assert result is not None
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.delete("/{txn_uuid}", status_code=204)
def abort_transaction(
    repo_name: str,
    txn_uuid: types.TxnUuid,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> None:
    txn: transaction.Transaction = _require_open_transaction(repo, txn_uuid)
    repo.abort(txn)


# ----------------------------------------------------------------------------------------------------------------------
@router.post("/revert")
def revert(
    repo_name: str,
    request: types.RevertRequest,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.CommitResponse:
    return repo.revert(request.target_seq, user=request.user, reason=request.reason)


# ----------------------------------------------------------------------------------------------------------------------
def _require_open_transaction(repo: repository.Repository, txn_uuid: types.TxnUuid) -> transaction.Transaction:
    # upload and abort cannot do anything without an open transaction, so an unknown id is a 404 here
    result: transaction.Transaction | None = repo.get_transaction(txn_uuid)
    if result is None:
        raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
    return result
