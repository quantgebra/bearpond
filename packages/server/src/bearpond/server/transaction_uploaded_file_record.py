import sqlalchemy
from sqlalchemy import orm

from bearpond.protocol import types
from . import base


# ======================================================================================================================
class TransactionUploadedFileRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "transaction_uploaded_files"
    __table_args__ = (
        sqlalchemy.ForeignKeyConstraint(
            ["repo", "txn_uuid"],
            ["bearpond.transactions.repo", "bearpond.transactions.txn_uuid"],
        ),
    )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_files_by_txn_uuid(session: orm.Session, repo_name: str, txn_uuid: str) -> list[types.FileMetadata]:
        statement: sqlalchemy.Select = (
            sqlalchemy.select(TransactionUploadedFileRecord)
            .where(
                TransactionUploadedFileRecord.repo_name == repo_name,
                TransactionUploadedFileRecord.txn_uuid == txn_uuid,
            )
            .order_by(TransactionUploadedFileRecord.path)
        )
        result: list[types.FileMetadata] = []
        for record in session.scalars(statement):
            result.append(types.FileMetadata(path=record.path, sha256=record.sha256, size=record.size))
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def record_file(session: orm.Session, repo_name: str, txn_uuid: str, file: types.FileMetadata) -> None:
        # a retried upload of the same path is idempotent: the declared hash is fixed at begin time, so a retry can only
        # ever rewrite the same row
        existing: TransactionUploadedFileRecord | None = session.get(
            TransactionUploadedFileRecord, (repo_name, txn_uuid, file.path)
        )
        if existing is None:
            session.add(
                TransactionUploadedFileRecord(
                    repo_name=repo_name, txn_uuid=txn_uuid, path=file.path, sha256=file.sha256, size=file.size
                )
            )
        else:
            existing.sha256 = file.sha256
            existing.size = file.size
        session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_txn_uuid(session: orm.Session, repo_name: str, txn_uuid: str) -> None:
        session.execute(
            sqlalchemy.delete(TransactionUploadedFileRecord).where(
                TransactionUploadedFileRecord.repo_name == repo_name,
                TransactionUploadedFileRecord.txn_uuid == txn_uuid,
            )
        )

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    repo_name: orm.Mapped[str] = orm.mapped_column("repo", primary_key=True)
    txn_uuid: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    path: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    sha256: orm.Mapped[str]
    size: orm.Mapped[int]
