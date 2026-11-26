from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from quantgebra.auth.core import auth_schema
from quantgebra.auth.local import login_throttle
from testcontainers.community import postgres

from . import auth_world
from . import oidc_world


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def postgres_engine() -> Iterator[sqlalchemy.Engine]:
    # one throwaway PostgreSQL for the whole run, with the migrations of the package applied; skipped where there is no
    # docker to start it with
    try:
        container: postgres.PostgresContainer = postgres.PostgresContainer("postgres:17-alpine", driver="psycopg")
        container.start()
    except Exception as error:
        pytest.skip(f"no PostgreSQL to test with: {error}")
    try:
        engine: sqlalchemy.Engine = sqlalchemy.create_engine(container.get_connection_url())
        auth_schema.upgrade(engine)
        yield engine
        engine.dispose()
    finally:
        container.stop()


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def engine(request: pytest.FixtureRequest) -> Iterator[sqlalchemy.Engine]:
    # the tests run on SQLite, which every machine has, and on PostgreSQL, which the service runs on
    result: sqlalchemy.Engine
    if request.param == "sqlite":
        result = auth_schema.make_sqlite_engine()
        auth_schema.upgrade(result)
        yield result
        result.dispose()
    else:
        result = request.getfixturevalue("postgres_engine")
        yield result
        _empty_tables(result)


# ----------------------------------------------------------------------------------------------------------------------
def _empty_tables(engine: sqlalchemy.Engine) -> None:
    # every test starts with nothing in the tables
    table_names: list[str] = []
    for table in auth_schema.get_metadata().sorted_tables:
        table_names.append(f"{auth_schema.SCHEMA_NAME}.{table.name}")
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text(f"TRUNCATE {', '.join(table_names)} RESTART IDENTITY CASCADE"))


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world_factory(engine: sqlalchemy.Engine, tmp_path: Path) -> Iterator[auth_world.WorldFactory]:
    worlds: list[auth_world.AuthWorld] = []

    def make_world(throttle: login_throttle.LoginThrottle | None = None) -> auth_world.AuthWorld:
        made_world: auth_world.AuthWorld = auth_world.AuthWorld(engine, tmp_path, throttle)
        worlds.append(made_world)
        return made_world

    yield make_world
    for each_world in worlds:
        each_world.close()


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(world_factory: auth_world.WorldFactory) -> auth_world.AuthWorld:
    return world_factory(None)


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def oidc(engine: sqlalchemy.Engine, tmp_path: Path) -> Iterator[oidc_world.OidcWorld]:
    made_world: oidc_world.OidcWorld = oidc_world.OidcWorld(engine, tmp_path)
    yield made_world
    made_world.close()
