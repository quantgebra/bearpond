import sqlalchemy
from sqlalchemy import orm

from . import auth_config
from . import base


# ======================================================================================================================
class AuthSettingsRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "auth_settings"
    __table_args__ = (sqlalchemy.CheckConstraint("singleton_id = 1", name="auth_settings_singleton"),)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_stored_settings(session: orm.Session) -> auth_config.StoredAuthSettings | None:
        record: AuthSettingsRecord | None = session.get(AuthSettingsRecord, 1)
        result: auth_config.StoredAuthSettings | None = None
        if record is not None:
            result = auth_config.StoredAuthSettings(
                session_duration_minutes=record.session_duration_minutes, is_local_enabled=record.is_local_enabled
            )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def save_stored_settings(session: orm.Session, settings: auth_config.StoredAuthSettings) -> None:
        record: AuthSettingsRecord | None = session.get(AuthSettingsRecord, 1)
        if record is None:
            record = AuthSettingsRecord(
                singleton_id=1,
                session_duration_minutes=settings.session_duration_minutes,
                is_local_enabled=settings.is_local_enabled,
            )
            session.add(record)
        else:
            record.session_duration_minutes = settings.session_duration_minutes
            record.is_local_enabled = settings.is_local_enabled
        session.flush()

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    singleton_id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    session_duration_minutes: orm.Mapped[int]
    is_local_enabled: orm.Mapped[bool]
