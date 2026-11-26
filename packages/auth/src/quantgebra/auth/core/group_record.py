from datetime import datetime

import sqlalchemy
from sqlalchemy import orm

from . import base
from . import utc_datetime


# ======================================================================================================================
class GroupRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "groups"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_group_name(session: orm.Session, group_name: str) -> "GroupRecord | None":
        return session.get(GroupRecord, group_name)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all(session: orm.Session) -> "list[GroupRecord]":
        statement: sqlalchemy.Select = sqlalchemy.select(GroupRecord).order_by(GroupRecord.group_name)
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_missing_group_names(session: orm.Session, group_names: list[str]) -> list[str]:
        # the names among the given ones that no group has
        existing_names: set[str] = set()
        for group in GroupRecord.get_all(session):
            existing_names.add(group.group_name)
        result: list[str] = []
        for group_name in group_names:
            if group_name not in existing_names:
                result.append(group_name)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def ensure_exist(session: orm.Session, group_names: list[str], now_datetime: datetime) -> None:
        # makes the groups among the given names that there are not yet
        for group_name in sorted(set(GroupRecord.get_missing_group_names(session, group_names))):
            group: GroupRecord = GroupRecord(group_name=group_name, description="", created_datetime=now_datetime)
            group.save(session)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_group_name(session: orm.Session, group_name: str) -> None:
        # removes the group; the users in it, the mappings to it and the rules that name it go with it
        session.execute(sqlalchemy.delete(GroupRecord).where(GroupRecord.group_name == group_name))

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    group_name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    description: orm.Mapped[str]
    created_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())

    # ------------------------------------------------------------------------------------------------------------------
    def save(self, session: orm.Session) -> None:
        session.add(self)
        session.flush()
