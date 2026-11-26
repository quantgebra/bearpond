from datetime import datetime
from typing import Literal

import sqlalchemy
from sqlalchemy import orm

from ..core import base
from ..core import user_record
from ..core import utc_datetime


# ======================================================================================================================
class InviteRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "invites"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_token_hash(session: orm.Session, token_hash: str) -> "InviteRecord | None":
        return session.get(InviteRecord, token_hash)

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    token_hash: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    user_id: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(user_record.UserRecord.user_id, ondelete="CASCADE"), index=True
    )
    purpose: orm.Mapped[Literal["invite", "reset"]] = orm.mapped_column(sqlalchemy.String(16))
    created_by: orm.Mapped[str]
    created_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
    expires_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
    used_datetime: orm.Mapped[datetime | None] = orm.mapped_column(utc_datetime.UtcDatetime())

    # ------------------------------------------------------------------------------------------------------------------
    def save(self, session: orm.Session) -> None:
        session.add(self)
        session.flush()
