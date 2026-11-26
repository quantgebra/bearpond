import sqlalchemy
from pydantic import TypeAdapter
from sqlalchemy import orm

from bearpond.protocol import types
from . import base
from . import repo_record
from . import transaction
from . import transaction_uploaded_file_record

_FILE_LIST_ADAPTER: TypeAdapter[list[types.FileMetadata]] = TypeAdapter(list[types.FileMetadata])
_UPDATED_LIST_ADAPTER: TypeAdapter[list[types.UpdatedFileMetadata]] = TypeAdapter(list[types.UpdatedFileMetadata])


# ======================================================================================================================
class TransactionRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "transactions"
    __table_args__ = (sqlalchemy.Index("transactions_repo_txn", "repo", "txn_uuid"),)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_transaction(session: orm.Session, repo_name: str, txn_uuid: str) -> transaction.Transaction | None:
        # the transaction as it is in flight, with the files that have been uploaded so far
        record: TransactionRecord | None = session.get(TransactionRecord, (repo_name, txn_uuid))
        result: transaction.Transaction | None = None
        if record is not None:
            result = transaction.Transaction(
                txn_uuid=types.TxnUuid(txn_uuid),
                created_at=record.created_at,
                added=_FILE_LIST_ADAPTER.validate_python(record.added),
                removed=_FILE_LIST_ADAPTER.validate_python(record.removed),
                updated=_UPDATED_LIST_ADAPTER.validate_python(record.updated),
                uploaded=transaction_uploaded_file_record.TransactionUploadedFileRecord.get_files_by_txn_uuid(
                    session, repo_name, txn_uuid
                ),
                user=record.user,
                reason=record.reason,
            )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def add_transaction(session: orm.Session, repo_name: str, txn: transaction.Transaction) -> None:
        session.add(
            TransactionRecord(
                repo_name=repo_name,
                txn_uuid=txn.txn_uuid,
                created_at=txn.created_at,
                added=[f.model_dump() for f in txn.added],
                removed=[f.model_dump() for f in txn.removed],
                updated=[f.model_dump() for f in txn.updated],
                user=txn.user,
                reason=txn.reason,
            )
        )
        session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_txn_uuid(session: orm.Session, repo_name: str, txn_uuid: str) -> None:
        # the files uploaded against the transaction go first, since they refer to it
        transaction_uploaded_file_record.TransactionUploadedFileRecord.delete_by_txn_uuid(session, repo_name, txn_uuid)
        session.execute(
            sqlalchemy.delete(TransactionRecord).where(
                TransactionRecord.repo_name == repo_name, TransactionRecord.txn_uuid == txn_uuid
            )
        )

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    repo_name: orm.Mapped[str] = orm.mapped_column(
        "repo", sqlalchemy.ForeignKey(repo_record.RepoRecord.name), primary_key=True
    )
    txn_uuid: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    created_at: orm.Mapped[str]
    added: orm.Mapped[list[dict[str, object]]] = orm.mapped_column(sqlalchemy.JSON)
    removed: orm.Mapped[list[dict[str, object]]] = orm.mapped_column(sqlalchemy.JSON)
    updated: orm.Mapped[list[dict[str, object]]] = orm.mapped_column(sqlalchemy.JSON)
    user: orm.Mapped[str | None]
    reason: orm.Mapped[str | None]
