import pytest
from quantgebra.auth.core import user_group_record
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import user_record
from quantgebra.auth.local import local_authentication

from . import auth_world


# ----------------------------------------------------------------------------------------------------------------------
def test_01_a_new_service_makes_the_account_admin_and_a_link_to_choose_its_password(
    world: auth_world.AuthWorld,
) -> None:

    outcome: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()

    assert outcome.kind == "link"
    assert outcome.username == "admin"
    assert outcome.warning is None
    assert outcome.link is not None
    assert outcome.link.url.startswith(f"{auth_world.BASE_URL}/invite/")
    users: list[user_record.UserRecord] = user_record.UserRecord.get_all(world.session)
    assert [user.username for user in users] == ["admin"]
    assert user_group_record.UserGroupRecord.get_group_names_by_user_id(world.session, users[0].user_id) == [
        "administrators"
    ]


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_account_cannot_log_in_until_its_owner_has_chosen_a_password(world: auth_world.AuthWorld) -> None:
    world.local.ensure_first_administrator()

    with pytest.raises(auth_error.AuthError) as refused:
        world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1")

    assert refused.value.kind == "invalid_credentials"


# ----------------------------------------------------------------------------------------------------------------------
def test_03_following_the_link_chooses_the_password_and_logs_the_owner_in(world: auth_world.AuthWorld) -> None:
    outcome: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()
    assert outcome.link is not None
    token: str = auth_world.token_of(outcome.link)

    info: local_authentication.InviteInfo | None = world.local.get_invite_info(token)
    login: auth_service.Login = world.local.accept_invite(token, auth_world.STRONG_PASSWORD)
    authenticated: auth_service.AuthenticatedRequest | None = world.auth.authenticate(login.token)

    assert info is not None
    assert info.username == "admin"
    assert info.purpose == "invite"
    assert authenticated is not None
    assert authenticated.actor.can("admin")


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_link_works_once(world: auth_world.AuthWorld) -> None:
    outcome: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()
    assert outcome.link is not None
    token: str = auth_world.token_of(outcome.link)
    world.local.accept_invite(token, auth_world.STRONG_PASSWORD)

    info: local_authentication.InviteInfo | None = world.local.get_invite_info(token)

    assert info is None


# ----------------------------------------------------------------------------------------------------------------------
def test_05_every_start_makes_a_new_link_until_the_first_one_has_been_used(world: auth_world.AuthWorld) -> None:
    first: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()

    second: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()

    assert first.link is not None
    assert second.kind == "link"
    assert second.link is not None
    assert second.link.url != first.link.url
    assert len(user_record.UserRecord.get_all(world.session)) == 1
    assert world.local.get_invite_info(auth_world.token_of(second.link)) is not None


# ----------------------------------------------------------------------------------------------------------------------
def test_06_once_the_password_is_chosen_a_start_changes_nothing(world: auth_world.AuthWorld) -> None:
    world.make_administrator()

    outcome: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()

    assert outcome.kind == "not_needed"
    assert len(user_record.UserRecord.get_all(world.session)) == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_07_a_warning_says_when_the_rules_leave_the_administrators_without_the_admin_permission(
    world: auth_world.AuthWorld,
) -> None:
    world.auth.config = world.config.with_settings(
        auth_config.StoredAuthSettings(session_duration_minutes=480, is_local_enabled=True), []
    )

    outcome: local_authentication.FirstAdministratorOutcome = world.local.ensure_first_administrator()

    assert outcome.warning is not None
    assert "admin" in outcome.warning


# ----------------------------------------------------------------------------------------------------------------------
def test_08_without_authentication_or_without_local_accounts_no_administrator_is_made(
    world_factory: auth_world.WorldFactory,
) -> None:
    development_world: auth_world.AuthWorld = world_factory(None)
    development_world.auth.config = auth_config.AuthConfig(
        "none", auth_world.BASE_URL, "administrators", auth_world.make_catalog(), 480, True, auth_world.make_rules()
    )
    provider_only_world: auth_world.AuthWorld = world_factory(None)
    provider_only_world.auth.config = provider_only_world.config.with_settings(
        auth_config.StoredAuthSettings(session_duration_minutes=480, is_local_enabled=False),
        provider_only_world.config.rules,
    )

    development_outcome: local_authentication.FirstAdministratorOutcome = (
        development_world.local.ensure_first_administrator()
    )
    provider_only_outcome: local_authentication.FirstAdministratorOutcome = (
        provider_only_world.local.ensure_first_administrator()
    )

    assert development_outcome.kind == "not_needed"
    assert provider_only_outcome.kind == "not_needed"
    assert user_record.UserRecord.count_all(development_world.session) == 0
