import pytest
import sqlalchemy
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from quantgebra.auth.core import auth_schema
from quantgebra.auth.core import user_group_record
from quantgebra.auth.core import group_record
from quantgebra.auth.core import session_record
from quantgebra.auth.core import user_record
from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import orm

from datetime import datetime, timezone

from . import auth_world


# ----------------------------------------------------------------------------------------------------------------------
def make_user(user_id: str, username: str, issuer: str = "local") -> user_record.UserRecord:
    return user_record.UserRecord(
        user_id=user_id,
        issuer=issuer,
        subject=user_id,
        username=username,
        display_name=username,
        email=None,
        is_disabled=False,
        created_datetime=datetime.now(timezone.utc),
    )


# ----------------------------------------------------------------------------------------------------------------------
def test_01_the_migrations_make_every_table_the_models_describe(engine: sqlalchemy.Engine) -> None:
    model_table_names: set[str] = set()
    for table in auth_schema.get_metadata().sorted_tables:
        model_table_names.add(table.name)

    with engine.connect() as connection:
        is_sqlite: bool = engine.dialect.name == "sqlite"
        migrated_table_names: set[str] = set(
            sqlalchemy.inspect(connection).get_table_names(schema=None if is_sqlite else auth_schema.SCHEMA_NAME)
        )
        migrated_table_names.discard(auth_schema.VERSION_TABLE_NAME)

        # PostgreSQL keeps the schema, so there the columns, indexes and constraints are compared too; SQLite has no
        # schemas for the models to be compared with
        differences: list = []
        if not is_sqlite:
            context: MigrationContext = MigrationContext.configure(
                connection,
                opts={
                    "target_metadata": auth_schema.get_metadata(),
                    "include_schemas": True,
                    "include_name": lambda name, type_, parent_names: type_ != "schema"
                    or name == auth_schema.SCHEMA_NAME,
                    "include_object": lambda obj, name, type_, reflected, compare_to: name
                    != auth_schema.VERSION_TABLE_NAME,
                },
            )
            differences = compare_metadata(context, auth_schema.get_metadata())

    assert migrated_table_names == model_table_names
    assert differences == []


# ----------------------------------------------------------------------------------------------------------------------
def test_02_running_the_migrations_again_changes_nothing(engine: sqlalchemy.Engine) -> None:
    auth_schema.upgrade(engine)

    with orm.Session(engine) as session:
        user_count: int = (
            session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(user_record.UserRecord)) or 0
        )

    assert user_count == 0


# ----------------------------------------------------------------------------------------------------------------------
def test_03_two_accounts_of_the_service_cannot_have_the_same_user_name(engine: sqlalchemy.Engine) -> None:
    with orm.Session(engine) as session:
        session.add(make_user("u1", "alice"))
        session.flush()
        session.add(make_user("u2", "alice"))

        with pytest.raises(sqlalchemy_exc.IntegrityError):
            session.flush()


# ----------------------------------------------------------------------------------------------------------------------
def test_04_people_that_a_provider_vouches_for_may_share_a_user_name(engine: sqlalchemy.Engine) -> None:
    with orm.Session(engine) as session:
        session.add(make_user("u1", "alice", "https://idp.example"))
        session.add(make_user("u2", "alice", "https://idp.example"))
        session.flush()
        user_count: int = (
            session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(user_record.UserRecord)) or 0
        )

    assert user_count == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_05_removing_a_user_removes_their_logins_and_group_memberships(engine: sqlalchemy.Engine) -> None:
    now_datetime = datetime.now(timezone.utc)
    with orm.Session(engine) as session:
        session.add(make_user("u1", "alice"))
        session.add(group_record.GroupRecord(group_name="writers", description="", created_datetime=now_datetime))
        session.flush()
        session.add(user_group_record.UserGroupRecord(user_id="u1", group_name="writers"))
        session.add(
            session_record.SessionRecord(
                token_hash="t1",
                user_id="u1",
                provider_name="local",
                provider_group_names=[],
                csrf_token="c1",
                created_datetime=now_datetime,
                expires_datetime=now_datetime,
            )
        )
        session.flush()

        session.execute(sqlalchemy.delete(user_record.UserRecord).where(user_record.UserRecord.user_id == "u1"))
        session_count: int = (
            session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(session_record.SessionRecord)) or 0
        )
        member_count: int = (
            session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(user_group_record.UserGroupRecord))
            or 0
        )

    assert session_count == 0
    assert member_count == 0


# ----------------------------------------------------------------------------------------------------------------------
def test_06_a_moment_comes_back_as_the_same_moment_in_utc(engine: sqlalchemy.Engine) -> None:
    now_datetime = datetime.now(timezone.utc)
    with orm.Session(engine) as session:
        session.add(make_user("u1", "alice"))
        session.flush()
        session.expire_all()
        loaded: user_record.UserRecord = session.get(user_record.UserRecord, "u1")  # type: ignore[assignment]

        assert loaded.created_datetime.tzinfo is not None
        assert abs((loaded.created_datetime - now_datetime).total_seconds()) < 5


# ----------------------------------------------------------------------------------------------------------------------
def test_07_a_moment_that_does_not_say_its_time_zone_is_refused(engine: sqlalchemy.Engine) -> None:
    naive_user: user_record.UserRecord = make_user("u1", "alice")
    naive_user.created_datetime = naive_user.created_datetime.replace(tzinfo=None)

    with orm.Session(engine) as session:
        session.add(naive_user)

        with pytest.raises(sqlalchemy_exc.StatementError):
            session.flush()
