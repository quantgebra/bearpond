from datetime import datetime

import sqlalchemy
from sqlalchemy import orm

from ..core import base
from ..core import user_record
from ..core import utc_datetime


# ======================================================================================================================
class LocalCredentialRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "local_credentials"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_user_id(session: orm.Session, user_id: str) -> "LocalCredentialRecord | None":
        return session.get(LocalCredentialRecord, user_id)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_user_ids_with_passwords(session: orm.Session, user_ids: list[str]) -> set[str]:
        # the ids among the given ones of the accounts that have a password
        statement: sqlalchemy.Select = sqlalchemy.select(LocalCredentialRecord.user_id).where(
            LocalCredentialRecord.user_id.in_(user_ids)
        )
        return set(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def set_password(session: orm.Session, user_id: str, password_hash: str, changed_datetime: datetime) -> None:
        # gives the account its first password, or changes the one that it has
        existing: LocalCredentialRecord | None = LocalCredentialRecord.get_by_user_id(session, user_id)
        if existing is None:
            session.add(
                LocalCredentialRecord(user_id=user_id, password_hash=password_hash, changed_datetime=changed_datetime)
            )
        else:
            existing.password_hash = password_hash
            existing.changed_datetime = changed_datetime
        session.flush()

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(user_record.UserRecord.user_id, ondelete="CASCADE"), primary_key=True
    )
    password_hash: orm.Mapped[str]
    changed_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
