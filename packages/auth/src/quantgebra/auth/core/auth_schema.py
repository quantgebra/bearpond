from pathlib import Path

import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

from . import base

SCHEMA_NAME: str = base.SCHEMA_NAME
VERSION_TABLE_NAME: str = "auth_alembic_version"
_MIGRATIONS_PATH: Path = Path(__file__).resolve().parent.parent / "migrations"


# ----------------------------------------------------------------------------------------------------------------------
def get_metadata() -> sqlalchemy.MetaData:
    # every table of authentication is registered once the modules that define them have been imported
    from ..local import invite_record, local_credential_record  # noqa: F401
    from ..oidc import oidc_provider_record  # noqa: F401
    from . import (  # noqa: F401
        auth_settings_record,
        grant_rule_record,
        group_mapping_record,
        group_record,
        provider_group_record,
        session_record,
        user_group_record,
        user_record,
    )

    return base.Base.metadata


# ----------------------------------------------------------------------------------------------------------------------
def make_sqlite_engine(url: str = "sqlite://", other_schema_names: list[str] | None = None) -> sqlalchemy.Engine:
    # an engine for SQLite, which has no schemas, so the schema of the tables is left out, and which only keeps to
    # foreign keys when told to on every connection; for local use and tests, where Postgres is not at hand
    engine: sqlalchemy.Engine = sqlalchemy.create_engine(
        url, connect_args={"check_same_thread": False}, poolclass=_get_sqlite_pool_class(url)
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    schema_translate_map: dict[str | None, str | None] = {SCHEMA_NAME: None}
    for other_schema_name in other_schema_names or []:
        schema_translate_map[other_schema_name] = None
    return engine.execution_options(schema_translate_map=schema_translate_map)


# ----------------------------------------------------------------------------------------------------------------------
def _get_sqlite_pool_class(url: str) -> type[sqlalchemy.pool.Pool]:
    # a database in memory exists once per connection, so every use has to share the one connection
    return sqlalchemy.pool.StaticPool if url in ("sqlite://", "sqlite:///:memory:") else sqlalchemy.pool.QueuePool


# ----------------------------------------------------------------------------------------------------------------------
def _enable_foreign_keys(connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA busy_timeout = 10000")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()


# ----------------------------------------------------------------------------------------------------------------------
def upgrade(engine: sqlalchemy.Engine) -> None:
    # brings the tables of authentication up to date, making them when there are none; the migrations are part of the
    # package, with a version table of their own, so they can run next to the application's
    get_metadata()
    config: Config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_PATH))
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(sqlalchemy.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


# ----------------------------------------------------------------------------------------------------------------------
def drop_all(engine: sqlalchemy.Engine) -> None:
    # takes the tables of authentication away, with the record of which migrations were run; for tests
    get_metadata()
    base.Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text(f"DROP TABLE IF EXISTS {_qualify(connection, VERSION_TABLE_NAME)}"))


# ----------------------------------------------------------------------------------------------------------------------
def _qualify(connection: sqlalchemy.Connection, table_name: str) -> str:
    return table_name if connection.dialect.name == "sqlite" else f"{SCHEMA_NAME}.{table_name}"
