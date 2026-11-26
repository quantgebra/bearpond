from pathlib import Path

import sqlalchemy
from alembic import command
from alembic.config import Config
from quantgebra.auth.core import auth_schema

from . import base

VERSION_TABLE_NAME: str = "bearpond_alembic_version"
_MIGRATIONS_PATH: Path = Path(__file__).resolve().parent / "migrations"


# ----------------------------------------------------------------------------------------------------------------------
def get_metadata() -> sqlalchemy.MetaData:
    # every table of the server is registered once the modules that define them have been imported
    from . import commit_record, object_record, repo_record, transaction_record  # noqa: F401
    from . import transaction_uploaded_file_record  # noqa: F401

    return base.Base.metadata


# ----------------------------------------------------------------------------------------------------------------------
def make_engine(database_url: str) -> sqlalchemy.Engine:
    # one engine for everything that the server keeps, its own tables and those of authentication; SQLite has no
    # schemas, so for it the schemas of both are left out of the tables
    result: sqlalchemy.Engine
    if database_url.startswith("sqlite"):
        result = auth_schema.make_sqlite_engine(database_url, [base.SCHEMA_NAME])
    else:
        result = sqlalchemy.create_engine(database_url)
    return result


# ----------------------------------------------------------------------------------------------------------------------
def upgrade(engine: sqlalchemy.Engine) -> None:
    # brings the tables of the server, and those of authentication, up to date, making them when there are none; a
    # database that an earlier version made with its own code is taken over as it is
    get_metadata()
    config: Config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_PATH))
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(sqlalchemy.text(f"CREATE SCHEMA IF NOT EXISTS {base.SCHEMA_NAME}"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    auth_schema.upgrade(engine)
