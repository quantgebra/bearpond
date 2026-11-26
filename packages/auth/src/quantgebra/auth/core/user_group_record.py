import sqlalchemy
from sqlalchemy import orm

from . import base
from . import group_record
from . import user_record


# ======================================================================================================================
class UserGroupRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "user_groups"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_group_names_by_user_id(session: orm.Session, user_id: str) -> list[str]:
        # the groups that an administrator put the user in, sorted
        statement: sqlalchemy.Select = (
            sqlalchemy.select(UserGroupRecord.group_name)
            .where(UserGroupRecord.user_id == user_id)
            .order_by(UserGroupRecord.group_name)
        )
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def count_by_group_name(session: orm.Session, group_name: str) -> int:
        statement: sqlalchemy.Select = (
            sqlalchemy.select(sqlalchemy.func.count())
            .select_from(UserGroupRecord)
            .where(UserGroupRecord.group_name == group_name)
        )
        return int(session.scalar(statement) or 0)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def replace_for_user(session: orm.Session, user_id: str, group_names: list[str]) -> None:
        # puts the user in exactly the given groups
        session.execute(sqlalchemy.delete(UserGroupRecord).where(UserGroupRecord.user_id == user_id))
        for group_name in sorted(set(group_names)):
            session.add(UserGroupRecord(user_id=user_id, group_name=group_name))
        session.flush()

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(user_record.UserRecord.user_id, ondelete="CASCADE"), primary_key=True
    )
    group_name: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(group_record.GroupRecord.group_name, ondelete="CASCADE"), primary_key=True, index=True
    )
