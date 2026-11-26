from datetime import datetime

import sqlalchemy
from sqlalchemy import orm

from . import base
from . import utc_datetime


# ======================================================================================================================
class ProviderGroupRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "provider_groups"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all(session: orm.Session) -> "list[ProviderGroupRecord]":
        statement: sqlalchemy.Select = sqlalchemy.select(ProviderGroupRecord).order_by(
            ProviderGroupRecord.provider_name, ProviderGroupRecord.provider_group_name
        )
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def record_seen(
        session: orm.Session, provider_name: str, provider_group_names: list[str], seen_datetime: datetime
    ) -> None:
        # records the groups that a provider sent with a login, so that they can be picked to be mapped
        for provider_group_name in provider_group_names:
            seen: ProviderGroupRecord | None = session.get(ProviderGroupRecord, (provider_name, provider_group_name))
            if seen is None:
                session.add(
                    ProviderGroupRecord(
                        provider_name=provider_name,
                        provider_group_name=provider_group_name,
                        seen_datetime=seen_datetime,
                    )
                )
            else:
                seen.seen_datetime = seen_datetime
        session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_provider_name(session: orm.Session, provider_name: str) -> None:
        session.execute(
            sqlalchemy.delete(ProviderGroupRecord).where(ProviderGroupRecord.provider_name == provider_name)
        )

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    provider_group_name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    seen_datetime: orm.Mapped[datetime] = orm.mapped_column(utc_datetime.UtcDatetime())
