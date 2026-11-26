from datetime import datetime, timezone
from pathlib import Path

import pytest
from quantgebra.auth.core import grant_rule_record
from quantgebra.auth.core import auth_settings_record
from quantgebra.auth.core import group_record
from quantgebra.auth import auth_services
from quantgebra.auth import auth_system
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import auth_settings_service
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from sqlalchemy import orm

from . import auth_world


# ----------------------------------------------------------------------------------------------------------------------
def authenticate_actor(world: auth_world.AuthWorld, login: auth_service.Login) -> actor.Actor:
    # the actor that a login stands for right now
    authenticated: auth_service.AuthenticatedRequest | None = world.auth.authenticate(login.token)
    assert authenticated is not None
    return authenticated.actor


# ----------------------------------------------------------------------------------------------------------------------
def test_01_a_user_in_no_group_may_do_nothing(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()

    login: auth_service.Login = world.make_user(administrator, "alice", [])
    alice: actor.Actor = authenticate_actor(world, login)

    assert alice.group_names == []
    assert not alice.can("read")
    assert not alice.can("write")
    assert not alice.can("admin")


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_rules_turn_the_groups_a_user_is_in_into_what_they_may_do(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.groups.create_group(administrator, "writers", "people who may write")

    login: auth_service.Login = world.make_user(administrator, "alice", ["writers"])
    alice: actor.Actor = authenticate_actor(world, login)

    assert alice.group_names == ["writers"]
    assert alice.can("write")
    assert not alice.can("admin")


# ----------------------------------------------------------------------------------------------------------------------
def test_03_the_administrator_permission_covers_everything_for_every_subject(world: auth_world.AuthWorld) -> None:

    administrator: actor.Actor = world.make_administrator()

    assert administrator.can("admin")
    assert administrator.can("write", "bearpond")
    assert administrator.can("read", "bullpen")


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_grant_can_be_limited_to_one_subject(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.groups.create_group(administrator, "bearpond-team", "")
    rules: list[grant_rule.GrantRule] = [
        grant_rule.GrantRule(group_name="administrators", grants=[grant.Grant(permission="admin")]),
        grant_rule.GrantRule(
            group_name="bearpond-team",
            grants=[grant.Grant(permission="write", subject="bearpond", subject_kind="repository")],
        ),
    ]
    world.settings.replace_rules(administrator, rules)
    login: auth_service.Login = world.make_user(administrator, "alice", ["bearpond-team"])

    alice: actor.Actor = authenticate_actor(world, login)

    assert alice.can("write", "bearpond")
    assert not alice.can("write", "bullpen")
    assert alice.can("read", "bullpen")


# ----------------------------------------------------------------------------------------------------------------------
def test_05_a_change_of_groups_takes_effect_on_the_next_request(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.groups.create_group(administrator, "writers", "")
    login: auth_service.Login = world.make_user(administrator, "alice", [])
    alice_id: str | None = world.local.find_user_id("alice")
    assert alice_id is not None
    assert not authenticate_actor(world, login).can("write")

    world.auth.set_user_groups(administrator, alice_id, ["writers"])

    assert authenticate_actor(world, login).can("write")


# ----------------------------------------------------------------------------------------------------------------------
def test_06_only_administrators_manage_users_and_groups(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    login: auth_service.Login = world.make_user(administrator, "alice", [])
    alice: actor.Actor = authenticate_actor(world, login)

    with pytest.raises(auth_error.AuthError) as listing:
        world.auth.list_users(alice)
    with pytest.raises(auth_error.AuthError) as creating:
        world.groups.create_group(alice, "sneaky", "")

    assert listing.value.kind == "forbidden"
    assert creating.value.kind == "forbidden"


# ----------------------------------------------------------------------------------------------------------------------
def test_07_a_user_cannot_be_put_in_a_group_that_does_not_exist(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.make_user(administrator, "alice", [])
    alice_id: str | None = world.local.find_user_id("alice")
    assert alice_id is not None

    with pytest.raises(auth_error.AuthError) as missing:
        world.auth.set_user_groups(administrator, alice_id, ["no-such-group"])

    assert missing.value.kind == "invalid_request"


# ----------------------------------------------------------------------------------------------------------------------
def test_08_disabling_a_user_ends_their_logins_and_stops_new_ones(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    login: auth_service.Login = world.make_user(administrator, "alice", [])
    alice_id: str | None = world.local.find_user_id("alice")
    assert alice_id is not None

    world.auth.set_user_disabled(administrator, alice_id, True)

    assert world.auth.authenticate(login.token) is None
    with pytest.raises(auth_error.AuthError):
        world.local.login("alice", auth_world.STRONG_PASSWORD, "10.0.0.1")


# ----------------------------------------------------------------------------------------------------------------------
def test_09_an_administrator_cannot_disable_their_own_account(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()

    with pytest.raises(auth_error.AuthError) as refused:
        world.auth.set_user_disabled(administrator, administrator.user_id, True)

    assert refused.value.kind == "invalid_request"


# ----------------------------------------------------------------------------------------------------------------------
def test_10_a_group_that_something_depends_on_cannot_be_removed(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.groups.create_group(administrator, "writers", "")
    world.make_user(administrator, "alice", ["writers"])

    with pytest.raises(auth_error.AuthError) as has_members:
        world.groups.delete_group(administrator, "writers")
    with pytest.raises(auth_error.AuthError) as is_administrators:
        world.groups.delete_group(administrator, "administrators")

    assert has_members.value.kind == "invalid_request"
    assert is_administrators.value.kind == "invalid_request"


# ----------------------------------------------------------------------------------------------------------------------
def test_11_rules_must_name_groups_and_permissions_that_exist(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    unknown_group: list[grant_rule.GrantRule] = [
        grant_rule.GrantRule(group_name="administrators", grants=[grant.Grant(permission="admin")]),
        grant_rule.GrantRule(group_name="no-such-group", grants=[grant.Grant(permission="write")]),
    ]
    unknown_permission: list[grant_rule.GrantRule] = [
        grant_rule.GrantRule(group_name="administrators", grants=[grant.Grant(permission="fly")]),
    ]
    wrong_subject_kind: list[grant_rule.GrantRule] = [
        grant_rule.GrantRule(
            group_name="administrators",
            grants=[grant.Grant(permission="admin", subject="bearpond", subject_kind="repository")],
        ),
    ]

    with pytest.raises(auth_error.AuthError) as group_error:
        world.settings.replace_rules(administrator, unknown_group)
    with pytest.raises(auth_error.AuthError) as permission_error:
        world.settings.replace_rules(administrator, unknown_permission)
    with pytest.raises(auth_error.AuthError) as kind_error:
        world.settings.replace_rules(administrator, wrong_subject_kind)

    assert group_error.value.kind == "invalid_request"
    assert permission_error.value.kind == "invalid_request"
    assert kind_error.value.kind == "invalid_request"


# ----------------------------------------------------------------------------------------------------------------------
def test_12_settings_that_would_leave_nobody_able_to_administer_are_refused(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    no_administrators: list[grant_rule.GrantRule] = [
        grant_rule.GrantRule(group_name="administrators", grants=[grant.Grant(permission="write")]),
    ]

    with pytest.raises(auth_error.AuthError) as rules_error:
        world.settings.replace_rules(administrator, no_administrators)
    with pytest.raises(auth_error.AuthError) as local_error:
        world.settings.update_settings(
            administrator, auth_config.StoredAuthSettings(session_duration_minutes=60, is_local_enabled=False)
        )

    assert "lock everyone out" in rules_error.value.message
    assert "lock everyone out" in local_error.value.message
    assert world.auth.config.is_local_enabled


# ----------------------------------------------------------------------------------------------------------------------
def test_13_settings_an_administrator_changes_are_kept_for_the_next_start(
    world: auth_world.AuthWorld, tmp_path: Path
) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.settings.update_settings(
        administrator, auth_config.StoredAuthSettings(session_duration_minutes=30, is_local_enabled=True)
    )
    world.session.commit()

    restarted_system: auth_system.AuthSystem = auth_system.AuthSystem(auth_world.make_config(), tmp_path)
    with orm.Session(world.engine, expire_on_commit=False) as restarted_session:
        restarted_services: auth_services.AuthServices = restarted_system.bind(restarted_session)
        restarted_services.settings.load_stored_settings()
        restarted_minutes: int = restarted_services.auth.config.session_duration_minutes

    assert restarted_minutes == 30
    assert world.config.session_duration_minutes == 480


# ----------------------------------------------------------------------------------------------------------------------
def test_14_a_stored_rule_for_a_permission_that_the_application_no_longer_has_is_reported(
    world: auth_world.AuthWorld,
) -> None:
    group_record.GroupRecord.ensure_exist(
        world.session, ["administrators", "pilots", "readers"], datetime.now(timezone.utc)
    )
    auth_settings_record.AuthSettingsRecord.save_stored_settings(
        world.session, auth_config.StoredAuthSettings(session_duration_minutes=480, is_local_enabled=True)
    )
    grant_rule_record.GrantRuleRecord.replace_all(
        world.session,
        [
            grant_rule.GrantRule(group_name="administrators", grants=[grant.Grant(permission="admin")]),
            grant_rule.GrantRule(group_name="pilots", grants=[grant.Grant(permission="fly")]),
            grant_rule.GrantRule(
                group_name="readers",
                grants=[grant.Grant(permission="admin", subject="alpha", subject_kind="repository")],
            ),
        ],
    )

    world.settings.load_stored_settings()
    dangling_grants: list[auth_settings_service.DanglingGrant] = world.settings.find_dangling_grants()

    assert [(each.group_name, each.grant.permission) for each in dangling_grants] == [
        ("pilots", "fly"),
        ("readers", "admin"),
    ]
    assert dangling_grants[0].problem == 'There is no permission "fly".'
    assert dangling_grants[1].problem == 'The permission "admin" cannot be limited to a repository.'


# ----------------------------------------------------------------------------------------------------------------------
def test_15_rules_that_agree_with_the_catalog_are_not_reported(world: auth_world.AuthWorld) -> None:

    assert world.settings.find_dangling_grants() == []
