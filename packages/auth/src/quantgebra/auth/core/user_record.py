from datetime import datetime

import sqlalchemy
from sqlalchemy import orm

from . import base
from . import utc_datetime


# ======================================================================================================================
class UserRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "users"
    __table_args__ = (
        sqlalchemy.UniqueConstraint("issuer", "subject", name="users_issuer_subject_unique"),
        # accounts of the service are known by their user name, so two cannot share one; for the people that a provider
        # vouches for, what the provider calls them can change and is not a key
        sqlalchemy.Index(
            "users_local_username_unique",
            "username",
            unique=True,
            postgresql_where=sqlalchemy.text("issuer = 'local'"),
            sqlite_where=sqlalchemy.text("issuer = 'local'"),
        ),
    )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_user_id(session: orm.Session, user_id: str) -> "UserRecord | None":
        return session.get(UserRecord, user_id)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_issuer_and_subject(session: orm.Session, issuer: str, subject: str) -> "UserRecord | None":
        statement: sqlalchemy.Select = sqlalchemy.select(UserRecord).where(
            UserRecord.issuer == issuer, UserRecord.subject == subject
        )
        return session.scalars(statement).first()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_issuer_and_username(session: orm.Session, issuer: str, username: str) -> "UserRecord | None":
        statement: sqlalchemy.Select = sqlalchemy.select(UserRecord).where(
            UserRecord.issuer == issuer, UserRecord.username == username
        )
        return session.scalars(statement).first()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all(session: orm.Session) -> "list[UserRecord]":
        statement: sqlalchemy.Select = sqlalchemy.select(UserRecord).order_by(UserRecord.username)
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def count_all(session: orm.Session) -> int:
        statement: sqlalchemy.Select = sqlalchemy.select(sqlalchemy.func.count()).select_from(UserRecord)
        return int(session.scalar(statement) or 0)

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    issuer: orm.Mapped[str]
    subject: orm.Mapped[str]
    username: orm.Mapped[str] = orm.mapped_column(index=True)
    display_name: orm.Mapped[str]
    email: orm.Mapped[str | None]
    is_disabled: orm.Mapped[bool]
    created_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
    last_login_datetime: orm.Mapped[datetime | None] = orm.mapped_column(utc_datetime.UtcDatetime())

    # ------------------------------------------------------------------------------------------------------------------
    def save(self, session: orm.Session) -> None:
        # a user that was loaded is saved by being changed, so this is for one that is new; it is flushed so that a user
        # name that is taken is found out now
        session.add(self)
        session.flush()
