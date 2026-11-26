from datetime import datetime, timedelta, timezone

import pytest
from quantgebra.auth.apikey import api_key_authentication
from quantgebra.auth.apikey import api_key_record
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import token_secrets
from quantgebra.auth.core import user_record

from . import auth_world


# ----------------------------------------------------------------------------------------------------------------------
def make_key(
    world: auth_world.AuthWorld, maker: actor.Actor, user_id: str, expires_in_days: int | None = None
) -> api_key_authentication.NewApiKey:
    request: api_key_authentication.ApiKeyRequest = api_key_authentication.ApiKeyRequest(
        user_id=user_id, key_name="ci-main", expires_in_days=expires_in_days
    )
    return world.services.api_keys.create_key(maker, request)


# ----------------------------------------------------------------------------------------------------------------------
def make_administrator(world: auth_world.AuthWorld) -> actor.Actor:
    # the first administrator, with the groups that the rules of the world name, as a server makes them when it starts
    administrator: actor.Actor = world.make_administrator()
    for group_name in ["readers", "writers"]:
        world.groups.create_group(administrator, group_name, "")
    return administrator


# ----------------------------------------------------------------------------------------------------------------------
def make_reader(world: auth_world.AuthWorld, administrator: actor.Actor) -> actor.Actor:
    # a person in the readers group, logged in
    login: auth_service.Login = world.make_user(administrator, "rita", ["readers"])
    authenticated: auth_service.AuthenticatedRequest | None = world.auth.authenticate(login.token)
    assert authenticated is not None
    return authenticated.actor


# ----------------------------------------------------------------------------------------------------------------------
def test_01_a_key_acts_as_its_user_with_the_groups_that_user_was_given(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    reader: actor.Actor = make_reader(world, administrator)

    new_key: api_key_authentication.NewApiKey = make_key(world, reader, reader.user_id)
    key_actor: actor.Actor | None = world.services.api_keys.authenticate(new_key.token)

    assert new_key.token.startswith("key_")
    assert new_key.key.prefix == new_key.token[:9]
    assert key_actor is not None
    assert key_actor.username == "rita"
    assert key_actor.group_names == ["readers"]
    assert key_actor.can("read")
    assert not key_actor.can("write")


# ----------------------------------------------------------------------------------------------------------------------
def test_02_only_a_hash_of_the_secret_is_kept(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)

    new_key: api_key_authentication.NewApiKey = make_key(world, administrator, administrator.user_id)
    stored: api_key_record.ApiKeyRecord | None = api_key_record.ApiKeyRecord.get_by_key_id(
        world.session, new_key.key.key_id
    )

    assert stored is not None
    assert stored.token_hash == token_secrets.hash_token(new_key.token)
    assert new_key.token not in stored.token_hash


# ----------------------------------------------------------------------------------------------------------------------
def test_03_a_secret_that_is_not_a_key_is_not_recognized(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    new_key: api_key_authentication.NewApiKey = make_key(world, administrator, administrator.user_id)

    assert world.services.api_keys.authenticate(new_key.token + "x") is None
    assert world.services.api_keys.authenticate("other_" + new_key.token) is None
    assert world.services.api_keys.authenticate("") is None


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_revoked_key_stops_working_at_once(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    new_key: api_key_authentication.NewApiKey = make_key(world, administrator, administrator.user_id)

    world.services.api_keys.revoke_key(administrator, new_key.key.key_id)

    assert world.services.api_keys.authenticate(new_key.token) is None
    assert world.services.api_keys.list_own_keys(administrator) == []


# ----------------------------------------------------------------------------------------------------------------------
def test_05_a_key_that_has_run_out_stops_working(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    new_key: api_key_authentication.NewApiKey = make_key(world, administrator, administrator.user_id, 30)
    stored: api_key_record.ApiKeyRecord | None = api_key_record.ApiKeyRecord.get_by_key_id(
        world.session, new_key.key.key_id
    )
    assert stored is not None

    stored.expires_datetime = datetime.now(timezone.utc) - timedelta(seconds=1)
    world.session.flush()

    assert world.services.api_keys.authenticate(new_key.token) is None
    assert world.services.api_keys.list_own_keys(administrator)[0].is_expired


# ----------------------------------------------------------------------------------------------------------------------
def test_06_a_key_of_a_disabled_user_does_not_work(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    reader: actor.Actor = make_reader(world, administrator)
    new_key: api_key_authentication.NewApiKey = make_key(world, reader, reader.user_id)

    world.auth.set_user_disabled(administrator, reader.user_id, True)

    assert world.services.api_keys.authenticate(new_key.token) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_07_people_make_and_revoke_their_own_keys_and_administrators_anyone_s(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    reader: actor.Actor = make_reader(world, administrator)
    administrator_key: api_key_authentication.NewApiKey = make_key(world, administrator, administrator.user_id)
    made_for_reader: api_key_authentication.NewApiKey = make_key(world, administrator, reader.user_id)

    with pytest.raises(auth_error.AuthError):
        make_key(world, reader, administrator.user_id)
    with pytest.raises(auth_error.AuthError):
        world.services.api_keys.revoke_key(reader, administrator_key.key.key_id)
    with pytest.raises(auth_error.AuthError):
        world.services.api_keys.list_all_keys(reader)
    world.services.api_keys.revoke_key(reader, made_for_reader.key.key_id)

    assert world.services.api_keys.list_own_keys(reader) == []
    assert len(world.services.api_keys.list_all_keys(administrator)) == 1


# ----------------------------------------------------------------------------------------------------------------------
def test_08_a_key_needs_a_name_and_lasts_a_sensible_number_of_days(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)

    for key_name, expires_in_days in [("  ", None), ("x" * 65, None), ("bell\x07", None), ("ok", 0), ("ok", 3651)]:
        request: api_key_authentication.ApiKeyRequest = api_key_authentication.ApiKeyRequest(
            user_id=administrator.user_id, key_name=key_name, expires_in_days=expires_in_days
        )
        with pytest.raises(auth_error.AuthError):
            world.services.api_keys.create_key(administrator, request)


# ----------------------------------------------------------------------------------------------------------------------
def test_09_using_a_key_is_noted_but_not_on_every_request(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    new_key: api_key_authentication.NewApiKey = make_key(world, administrator, administrator.user_id)

    world.services.api_keys.authenticate(new_key.token)
    first_use: datetime | None = world.services.api_keys.list_own_keys(administrator)[0].last_used_datetime
    world.services.api_keys.authenticate(new_key.token)
    second_use: datetime | None = world.services.api_keys.list_own_keys(administrator)[0].last_used_datetime

    assert new_key.key.last_used_datetime is None
    assert first_use is not None
    assert second_use == first_use


# ----------------------------------------------------------------------------------------------------------------------
def test_10_a_service_account_cannot_log_in_and_is_reached_with_its_keys(world: auth_world.AuthWorld) -> None:
    administrator: actor.Actor = make_administrator(world)
    request: api_key_authentication.ServiceAccountRequest = api_key_authentication.ServiceAccountRequest(
        username="CI", display_name="Nightly ETL", group_names=["writers"]
    )

    account: user_record.UserRecord = world.services.api_keys.create_service_account(administrator, request)
    new_key: api_key_authentication.NewApiKey = make_key(world, administrator, account.user_id)
    key_actor: actor.Actor | None = world.services.api_keys.authenticate(new_key.token)

    assert account.username == "ci"
    assert account.issuer == "service"
    assert key_actor is not None
    assert key_actor.can("write")
    assert new_key.key.is_owner_service_account
    with pytest.raises(auth_error.AuthError):
        world.local.login("ci", auth_world.STRONG_PASSWORD, "10.0.0.1")
    with pytest.raises(auth_error.AuthError):
        world.services.api_keys.create_service_account(administrator, request)


# ----------------------------------------------------------------------------------------------------------------------
def test_11_only_administrators_make_service_accounts_and_only_in_groups_that_exist(
    world: auth_world.AuthWorld,
) -> None:
    administrator: actor.Actor = make_administrator(world)
    reader: actor.Actor = make_reader(world, administrator)

    with pytest.raises(auth_error.AuthError):
        world.services.api_keys.create_service_account(
            reader, api_key_authentication.ServiceAccountRequest(username="ci", display_name="", group_names=[])
        )
    with pytest.raises(auth_error.AuthError):
        world.services.api_keys.create_service_account(
            administrator,
            api_key_authentication.ServiceAccountRequest(username="ci", display_name="", group_names=["nonexistent"]),
        )
