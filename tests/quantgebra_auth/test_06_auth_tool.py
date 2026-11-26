import logging
from pathlib import Path

import pytest
import sqlalchemy
from quantgebra.auth.core import user_group_record
from quantgebra.auth import auth_services
from quantgebra.auth import auth_system
from quantgebra.auth import auth_tool
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from quantgebra.auth.core import group_record
from quantgebra.auth.local import local_authentication
from sqlalchemy import orm

from . import auth_world


# ----------------------------------------------------------------------------------------------------------------------
def test_01_the_command_makes_the_first_administrator_and_says_the_link(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    system: auth_system.AuthSystem = auth_system.AuthSystem(auth_world.make_config(), tmp_path)
    lines: list[str] = []
    tool: auth_tool.AuthTool = auth_tool.AuthTool(system, engine, lines.append)

    exit_code: int = tool.run(["first-administrator"])

    assert exit_code == 0
    assert any(f"{auth_world.BASE_URL}/invite/" in line for line in lines)


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_command_makes_an_account_in_groups_and_a_reset_link_for_it(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    system: auth_system.AuthSystem = auth_system.AuthSystem(auth_world.make_config(), tmp_path)
    system.start(engine)
    lines: list[str] = []
    tool: auth_tool.AuthTool = auth_tool.AuthTool(system, engine, lines.append)

    created: int = tool.run(["create-invite", "alice", "--display-name", "Alice", "--group", "writers"])
    reset: int = tool.run(["reset-link", "alice"])

    with orm.Session(engine) as session:
        services: auth_services.AuthServices = system.bind(session)
        alice_id: str | None = services.local.find_user_id("alice")
        assert alice_id is not None
        alice_group_names: list[str] = user_group_record.UserGroupRecord.get_group_names_by_user_id(session, alice_id)
    assert created == 0
    assert reset == 0
    assert alice_group_names == ["writers"]


# ----------------------------------------------------------------------------------------------------------------------
def test_03_the_command_says_what_went_wrong_and_exits_with_one(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    system: auth_system.AuthSystem = auth_system.AuthSystem(auth_world.make_config(), tmp_path)
    lines: list[str] = []
    tool: auth_tool.AuthTool = auth_tool.AuthTool(system, engine, lines.append)

    exit_code: int = tool.run(["reset-link", "nobody"])

    assert exit_code == 1
    assert lines == ['error: There is no account named "nobody".']


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_second_start_of_the_command_after_the_password_was_chosen_has_nothing_to_do(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    system: auth_system.AuthSystem = auth_system.AuthSystem(auth_world.make_config(), tmp_path)
    outcome: local_authentication.FirstAdministratorOutcome = system.start(engine)
    assert outcome.link is not None
    with orm.Session(engine, expire_on_commit=False) as session:
        system.bind(session).local.accept_invite(auth_world.token_of(outcome.link), auth_world.STRONG_PASSWORD)
        session.commit()
    lines: list[str] = []
    tool: auth_tool.AuthTool = auth_tool.AuthTool(system, engine, lines.append)

    exit_code: int = tool.run(["first-administrator"])

    assert exit_code == 0
    assert lines == ["There is nothing to do: the service has its accounts."]


# ----------------------------------------------------------------------------------------------------------------------
def test_05_starting_the_system_makes_the_groups_that_the_rules_name(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    system: auth_system.AuthSystem = auth_system.AuthSystem(auth_world.make_config(), tmp_path)

    system.start(engine)

    with orm.Session(engine) as session:
        groups: list[group_record.GroupRecord] = group_record.GroupRecord.get_all(session)
        group_names: list[str] = [group.group_name for group in groups]
    assert group_names == ["administrators", "readers", "writers"]


# ----------------------------------------------------------------------------------------------------------------------
def test_06_starting_the_system_says_which_rules_give_a_permission_that_does_not_exist(
    engine: sqlalchemy.Engine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rules: list[grant_rule.GrantRule] = auth_world.make_rules()
    rules.append(grant_rule.GrantRule(group_name="pilots", grants=[grant.Grant(permission="fly")]))
    config: auth_config.AuthConfig = auth_config.AuthConfig(
        "enforced", auth_world.BASE_URL, "administrators", auth_world.make_catalog(), 480, True, rules
    )
    system: auth_system.AuthSystem = auth_system.AuthSystem(config, tmp_path)

    with caplog.at_level(logging.WARNING, logger="quantgebra.auth.auth_system"):
        system.start(engine)

    assert 'the rule for the group pilots gives fly, which does nothing: There is no permission "fly".' in caplog.text
