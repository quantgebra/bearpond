import pytest
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import user_record
from quantgebra.auth.local import local_authentication
from quantgebra.auth.local import login_throttle

from . import auth_world


# ----------------------------------------------------------------------------------------------------------------------
def test_01_a_right_password_logs_in_and_the_login_is_recognized(world: auth_world.AuthWorld) -> None:
    world.make_administrator()

    login: auth_service.Login = world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1")
    authenticated: auth_service.AuthenticatedRequest | None = world.auth.authenticate(login.token)

    assert authenticated is not None
    assert authenticated.actor.username == "admin"
    assert authenticated.actor.issuer == "local"
    assert authenticated.actor.group_names == ["administrators"]


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_user_name_is_not_case_sensitive(world: auth_world.AuthWorld) -> None:
    world.make_administrator()

    login: auth_service.Login = world.local.login("  ADMIN ", auth_world.STRONG_PASSWORD, "10.0.0.1")

    assert world.auth.authenticate(login.token) is not None


# ----------------------------------------------------------------------------------------------------------------------
def test_03_a_wrong_password_and_an_unknown_user_name_are_refused_in_the_same_words(
    world: auth_world.AuthWorld,
) -> None:
    world.make_administrator()

    with pytest.raises(auth_error.AuthError) as wrong_password:
        world.local.login("admin", "not the password at all", "10.0.0.1")
    with pytest.raises(auth_error.AuthError) as unknown_user:
        world.local.login("nobody", "not the password at all", "10.0.0.1")

    assert wrong_password.value.kind == "invalid_credentials"
    assert unknown_user.value.message == wrong_password.value.message


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_user_name_is_locked_for_a_while_after_too_many_wrong_passwords(
    world_factory: auth_world.WorldFactory,
) -> None:
    world: auth_world.AuthWorld = world_factory(login_throttle.LoginThrottle(3, 30))
    world.make_administrator()
    for attempt_number in range(3):
        with pytest.raises(auth_error.AuthError):
            world.local.login("admin", f"wrong password number {attempt_number}", "10.0.0.1")

    with pytest.raises(auth_error.AuthError) as locked:
        world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1")

    assert locked.value.kind == "throttled"


# ----------------------------------------------------------------------------------------------------------------------
def test_05_an_address_is_locked_for_a_while_after_too_many_wrong_passwords_for_any_names(
    world_factory: auth_world.WorldFactory,
) -> None:
    world: auth_world.AuthWorld = world_factory(login_throttle.LoginThrottle(5, 4))
    world.make_administrator()
    for attempt_number in range(4):
        with pytest.raises(auth_error.AuthError):
            world.local.login(f"nobody{attempt_number}", "not the password at all", "10.0.0.9")

    with pytest.raises(auth_error.AuthError) as locked:
        world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.9")

    assert locked.value.kind == "throttled"


# ----------------------------------------------------------------------------------------------------------------------
def test_06_logging_out_ends_the_login(world: auth_world.AuthWorld) -> None:
    world.make_administrator()
    login: auth_service.Login = world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1")

    world.auth.logout(login.token)

    assert world.auth.authenticate(login.token) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_07_a_secret_that_is_not_a_login_is_not_recognized(world: auth_world.AuthWorld) -> None:
    world.make_administrator()

    assert world.auth.authenticate("not-a-secret") is None


# ----------------------------------------------------------------------------------------------------------------------
def test_08_a_login_that_has_run_out_is_not_recognized(world: auth_world.AuthWorld) -> None:
    world.make_administrator()
    login: auth_service.Login = world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1")
    login.session.expires_datetime = login.session.created_datetime.replace(year=2000)
    world.session.flush()

    assert world.auth.authenticate(login.token) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_09_a_password_that_is_too_short_or_the_user_name_is_refused_when_it_is_chosen(
    world: auth_world.AuthWorld,
) -> None:
    administrator: actor.Actor = world.make_administrator()
    request: local_authentication.InviteRequest = local_authentication.InviteRequest(
        username="alice-in-wonderland", display_name="Alice", group_names=[]
    )
    link: local_authentication.InviteLink = world.local.invite(administrator, request)

    with pytest.raises(auth_error.AuthError) as too_short:
        world.local.accept_invite(auth_world.token_of(link), "short")
    with pytest.raises(auth_error.AuthError) as same_as_name:
        world.local.accept_invite(auth_world.token_of(link), "Alice-In-Wonderland")

    assert too_short.value.kind == "weak_password"
    assert same_as_name.value.kind == "weak_password"
    assert world.local.get_invite_info(auth_world.token_of(link)) is not None


# ----------------------------------------------------------------------------------------------------------------------
def test_10_changing_the_password_needs_the_present_one_and_ends_the_other_logins(world: auth_world.AuthWorld) -> None:
    world.make_administrator()
    first_login: auth_service.Login = world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1")
    second_login: auth_service.Login = world.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.2")
    second_request: auth_service.AuthenticatedRequest | None = world.auth.authenticate(second_login.token)
    assert second_request is not None

    with pytest.raises(auth_error.AuthError) as not_the_present_one:
        world.local.change_password(
            second_request.actor, second_request.session, "not the present one", "a brand new long password"
        )
    world.local.change_password(
        second_request.actor, second_request.session, auth_world.STRONG_PASSWORD, "a brand new long password"
    )

    assert not_the_present_one.value.kind == "invalid_credentials"
    assert world.auth.authenticate(first_login.token) is None
    assert world.auth.authenticate(second_login.token) is not None
    assert world.local.login("admin", "a brand new long password", "10.0.0.1") is not None


# ----------------------------------------------------------------------------------------------------------------------
def test_11_an_administrator_invites_a_person_and_makes_a_link_to_reset_a_password(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    world.make_user(administrator, "alice", [])
    alice_id: str | None = world.local.find_user_id("alice")
    assert alice_id is not None

    link: local_authentication.InviteLink = world.local.reset_link(administrator, alice_id)
    info: local_authentication.InviteInfo | None = world.local.get_invite_info(auth_world.token_of(link))

    assert info is not None
    assert info.purpose == "reset"
    assert info.username == "alice"


# ----------------------------------------------------------------------------------------------------------------------
def test_12_only_an_administrator_may_invite_or_reset(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    alice_login: auth_service.Login = world.make_user(administrator, "alice", [])
    alice_request: auth_service.AuthenticatedRequest | None = world.auth.authenticate(alice_login.token)
    assert alice_request is not None
    request: local_authentication.InviteRequest = local_authentication.InviteRequest(
        username="bob", display_name="Bob", group_names=[]
    )

    with pytest.raises(auth_error.AuthError) as not_allowed:
        world.local.invite(alice_request.actor, request)

    assert not_allowed.value.kind == "forbidden"


# ----------------------------------------------------------------------------------------------------------------------
def test_13_a_user_name_that_is_taken_or_not_a_user_name_is_refused(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = world.make_administrator()
    taken: local_authentication.InviteRequest = local_authentication.InviteRequest(
        username="admin", display_name="Another", group_names=[]
    )
    invalid: local_authentication.InviteRequest = local_authentication.InviteRequest(
        username="Not A Name!", display_name="Invalid", group_names=[]
    )

    with pytest.raises(auth_error.AuthError) as already_there:
        world.local.invite(administrator, taken)
    with pytest.raises(auth_error.AuthError) as not_a_name:
        world.local.invite(administrator, invalid)

    assert already_there.value.kind == "invalid_request"
    assert not_a_name.value.kind == "invalid_request"
    assert len(user_record.UserRecord.get_all(world.session)) == 1
    assert isinstance(user_record.UserRecord.get_all(world.session)[0], user_record.UserRecord)
