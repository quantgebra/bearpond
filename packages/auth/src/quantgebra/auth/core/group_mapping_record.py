import sqlalchemy
from sqlalchemy import orm

from . import base
from . import group_record


# ======================================================================================================================
class GroupMappingRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "group_mappings"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all(session: orm.Session) -> "list[GroupMappingRecord]":
        statement: sqlalchemy.Select = sqlalchemy.select(GroupMappingRecord).order_by(
            GroupMappingRecord.provider_name, GroupMappingRecord.provider_group_name, GroupMappingRecord.group_name
        )
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_group_names_by_provider_groups(
        session: orm.Session, provider_name: str, provider_group_names: list[str]
    ) -> list[str]:
        # the groups that the groups of a provider that a person is in are mapped to, sorted
        group_names: list[str] = []
        if provider_group_names:
            statement: sqlalchemy.Select = (
                sqlalchemy.select(GroupMappingRecord.group_name)
                .where(
                    GroupMappingRecord.provider_name == provider_name,
                    GroupMappingRecord.provider_group_name.in_(provider_group_names),
                )
                .distinct()
                .order_by(GroupMappingRecord.group_name)
            )
            group_names = list(session.scalars(statement))
        return group_names

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def count_by_group_name(session: orm.Session, group_name: str) -> int:
        statement: sqlalchemy.Select = (
            sqlalchemy.select(sqlalchemy.func.count())
            .select_from(GroupMappingRecord)
            .where(GroupMappingRecord.group_name == group_name)
        )
        return int(session.scalar(statement) or 0)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_provider_name(session: orm.Session, provider_name: str) -> None:
        session.execute(sqlalchemy.delete(GroupMappingRecord).where(GroupMappingRecord.provider_name == provider_name))

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    provider_group_name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    group_name: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(group_record.GroupRecord.group_name, ondelete="CASCADE"), primary_key=True, index=True
    )

    # ------------------------------------------------------------------------------------------------------------------
    def save_if_absent(self, session: orm.Session) -> None:
        key: tuple[str, str, str] = (self.provider_name, self.provider_group_name, self.group_name)
        if session.get(GroupMappingRecord, key) is None:
            session.add(self)
            session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    def delete(self, session: orm.Session) -> None:
        session.execute(
            sqlalchemy.delete(GroupMappingRecord).where(
                GroupMappingRecord.provider_name == self.provider_name,
                GroupMappingRecord.provider_group_name == self.provider_group_name,
                GroupMappingRecord.group_name == self.group_name,
            )
        )
