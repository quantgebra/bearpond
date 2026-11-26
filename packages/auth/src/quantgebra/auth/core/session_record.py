from datetime import datetime

import sqlalchemy
from sqlalchemy import orm

from . import base
from . import user_record
from . import utc_datetime


# ======================================================================================================================
class SessionRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "sessions"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_token_hash(session: orm.Session, token_hash: str) -> "SessionRecord | None":
        return session.get(SessionRecord, token_hash)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_token_hash(session: orm.Session, token_hash: str) -> None:
        session.execute(sqlalchemy.delete(SessionRecord).where(SessionRecord.token_hash == token_hash))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_user_id_keeping(session: orm.Session, user_id: str, keep_token_hash: str | None) -> None:
        # ends every login of the user, except the one with the hash when there is one to keep
        statement: sqlalchemy.Delete = sqlalchemy.delete(SessionRecord).where(SessionRecord.user_id == user_id)
        if keep_token_hash is not None:
            statement = statement.where(SessionRecord.token_hash != keep_token_hash)
        session.execute(statement)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_expired_before(session: orm.Session, now_datetime: datetime) -> None:
        session.execute(sqlalchemy.delete(SessionRecord).where(SessionRecord.expires_datetime < now_datetime))

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    token_hash: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    user_id: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(user_record.UserRecord.user_id, ondelete="CASCADE"), index=True
    )
    provider_name: orm.Mapped[str]
    provider_group_names: orm.Mapped[list[str]] = orm.mapped_column(sqlalchemy.JSON)
    csrf_token: orm.Mapped[str]
    created_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
    expires_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime(), index=True)

    # ------------------------------------------------------------------------------------------------------------------
    def save(self, session: orm.Session) -> None:
        session.add(self)
        session.flush()
