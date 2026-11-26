from datetime import datetime

import sqlalchemy
from sqlalchemy import orm

from ..core import base
from ..core import user_record
from ..core import utc_datetime


# ======================================================================================================================
class ApiKeyRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "api_keys"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_key_id(session: orm.Session, key_id: str) -> "ApiKeyRecord | None":
        return session.get(ApiKeyRecord, key_id)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_token_hash(session: orm.Session, token_hash: str) -> "ApiKeyRecord | None":
        statement: sqlalchemy.Select = sqlalchemy.select(ApiKeyRecord).where(ApiKeyRecord.token_hash == token_hash)
        return session.scalars(statement).first()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all_by_user_id(session: orm.Session, user_id: str) -> "list[ApiKeyRecord]":
        # the keys that act as the user, newest first
        statement: sqlalchemy.Select = (
            sqlalchemy.select(ApiKeyRecord)
            .where(ApiKeyRecord.user_id == user_id)
            .order_by(ApiKeyRecord.created_datetime.desc())
        )
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all(session: orm.Session) -> "list[ApiKeyRecord]":
        statement: sqlalchemy.Select = sqlalchemy.select(ApiKeyRecord).order_by(ApiKeyRecord.created_datetime.desc())
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_key_id(session: orm.Session, key_id: str) -> None:
        session.execute(sqlalchemy.delete(ApiKeyRecord).where(ApiKeyRecord.key_id == key_id))

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    # what the pages revoke the key by; the secret is never in it
    key_id: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    # only a hash of the secret is kept, so the database cannot be used to act as the user
    token_hash: orm.Mapped[str] = orm.mapped_column(unique=True)
    # the first characters of the secret, which are all that is shown of it after it was made
    prefix: orm.Mapped[str]
    # what the key is for, which the person who made it chose, such as "ci-main"
    key_name: orm.Mapped[str]
    # the user that the key acts as; the key goes with the user
    user_id: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(user_record.UserRecord.user_id, ondelete="CASCADE"), index=True
    )
    created_by_user_id: orm.Mapped[str]
    created_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
    # None for a key that never stops working
    expires_datetime: orm.Mapped[datetime | None] = orm.mapped_column(utc_datetime.UtcDatetime())
    # to within a few minutes, None before the first use
    last_used_datetime: orm.Mapped[datetime | None] = orm.mapped_column(utc_datetime.UtcDatetime())

    # ------------------------------------------------------------------------------------------------------------------
    def save(self, session: orm.Session) -> None:
        session.add(self)
        session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    def is_expired(self, now_datetime: datetime) -> bool:
        return self.expires_datetime is not None and self.expires_datetime <= now_datetime
