from datetime import datetime, timezone

import sqlalchemy
from sqlalchemy.engine import Dialect


# ======================================================================================================================
class UtcDatetime(sqlalchemy.TypeDecorator):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    impl = sqlalchemy.DateTime(timezone=True)
    cache_ok = True

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        # only moments that say which time zone they are in are stored, and they are stored as UTC, so that what comes
        # back means the same thing on a database that keeps the time zone and on one that does not
        result: datetime | None = None
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("a datetime has to say which time zone it is in")
            result = value.astimezone(timezone.utc)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        # SQLite gives back what it stored without the time zone, which is UTC
        result: datetime | None = value
        if value is not None and value.tzinfo is None:
            result = value.replace(tzinfo=timezone.utc)
        return result
