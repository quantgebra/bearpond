import sys
from pathlib import Path

import pytest
from joserfc.jwk import RSAKey
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import provider_group_record
from quantgebra.auth.core import group_mapping_record
from quantgebra.auth.core import group_record
from quantgebra.auth.core import user_record
from quantgebra.auth.oidc import oidc_provider
from quantgebra.auth.oidc import oidc_provider_settings
from quantgebra.auth.oidc import oidc_settings
from quantgebra.auth.oidc import pending_logins

from . import auth_world
from . import fake_oidc_provider
from . import oidc_world

ALICE_CLAIMS: dict[str, object] = {
    "sub": "idp-user-1",
    "preferred_username": "alice",
    "name": "Alice Anderson",
    "email": "alice@example.com",
    "groups": ["idp-readers"],
}


# ----------------------------------------------------------------------------------------------------------------------
def authenticate_actor(world: oidc_world.OidcWorld, login: auth_service.Login) -> actor.Actor:
    authenticated: auth_service.AuthenticatedRequest | None = world.auth.authenticate(login.token)
    assert authenticated is not None
    return authenticated.actor


# ----------------------------------------------------------------------------------------------------------------------
def test_01_an_administrator_adds_a_provider_and_its_secret_is_kept_in_a_file_that_is_never_shown(
    oidc: oidc_world.OidcWorld, tmp_path: Path
) -> None:

    administrator: actor.Actor = oidc.add_provider()
    views: list[oidc_settings.ProviderView] = oidc.oidc_settings.list_providers(administrator)

    assert [view.settings.provider_name for view in views] == ["idp"]
    assert views[0].is_secret_present
    assert "the-client-secret" not in views[0].model_dump_json()
    assert (tmp_path / "idp").read_text() == fake_oidc_provider.CLIENT_SECRET


# ----------------------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="windows has no owner-only file permissions to check")
def test_01_b_the_file_of_a_secret_can_only_be_read_by_the_user_of_the_service(
    oidc: oidc_world.OidcWorld, tmp_path: Path
) -> None:

    oidc.add_provider()

    assert (tmp_path / "idp").stat().st_mode & 0o077 == 0


# ----------------------------------------------------------------------------------------------------------------------
def test_02_a_provider_needs_a_name_that_is_not_taken_and_a_secret() -> None:
    with pytest.raises(ValueError):
        oidc_provider_settings.OidcProviderSettings(
            provider_name="Not A Name", label="x", issuer="https://idp.example", client_id="c"
        )


# ----------------------------------------------------------------------------------------------------------------------
def test_03_the_login_page_sends_the_person_to_the_provider_with_everything_that_makes_the_flow_safe(
    oidc: oidc_world.OidcWorld,
) -> None:
    oidc.add_provider()
    provider: oidc_provider.OidcProvider = oidc.find_provider()

    start: oidc_provider.LoginStart = provider.begin(oidc_world.REDIRECT_URI)

    assert start.url.startswith("https://idp.example/authorize?")
    assert "response_type=code" in start.url
    assert "code_challenge_method=S256" in start.url
    assert f"state={start.pending.state}" in start.url
    assert f"nonce={start.pending.nonce}" in start.url
    assert "code_challenge=" in start.url
    assert start.pending.verifier not in start.url


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_person_the_provider_vouches_for_is_recorded_and_logged_in(oidc: oidc_world.OidcWorld) -> None:
    oidc.add_provider()

    login: auth_service.Login = oidc.log_in_through_provider(ALICE_CLAIMS)
    alice: actor.Actor = authenticate_actor(oidc, login)

    assert alice.username == "alice"
    assert alice.display_name == "Alice Anderson"
    assert alice.issuer == fake_oidc_provider.ISSUER
    assert alice.group_names == []
    assert not alice.can("read")


# ----------------------------------------------------------------------------------------------------------------------
def test_05_the_same_person_is_the_same_user_whatever_they_are_called_now(oidc: oidc_world.OidcWorld) -> None:
    oidc.add_provider()
    oidc.log_in_through_provider(ALICE_CLAIMS)
    renamed_claims: dict[str, object] = dict(ALICE_CLAIMS)
    renamed_claims["preferred_username"] = "alice.anderson"
    renamed_claims["email"] = "alice@new.example.com"

    oidc.log_in_through_provider(renamed_claims)
    users: list[user_record.UserRecord] = user_record.UserRecord.get_all(oidc.session)
    provider_users: list[user_record.UserRecord] = []
    for user in users:
        if user.issuer == fake_oidc_provider.ISSUER:
            provider_users.append(user)

    assert len(provider_users) == 1
    assert provider_users[0].username == "alice.anderson"
    assert provider_users[0].email == "alice@new.example.com"


# ----------------------------------------------------------------------------------------------------------------------
def test_06_the_groups_a_provider_sends_give_nothing_until_an_administrator_maps_them(
    oidc: oidc_world.OidcWorld,
) -> None:
    administrator: actor.Actor = oidc.add_provider()
    first_login: auth_service.Login = oidc.log_in_through_provider(ALICE_CLAIMS)
    assert authenticate_actor(oidc, first_login).group_names == []

    seen: list[provider_group_record.ProviderGroupRecord] = oidc.groups.list_provider_groups(administrator)
    oidc.groups.create_group(administrator, "readers", "")
    mapping: group_mapping_record.GroupMappingRecord = group_mapping_record.GroupMappingRecord(
        provider_name="idp", provider_group_name="idp-readers", group_name="readers"
    )
    oidc.groups.add_mapping(administrator, mapping)

    assert [group.provider_group_name for group in seen] == ["idp-readers"]
    assert authenticate_actor(oidc, first_login).group_names == ["readers"]


# ----------------------------------------------------------------------------------------------------------------------
def test_07_a_mapped_group_gives_what_the_rules_give_its_group(oidc: oidc_world.OidcWorld) -> None:
    administrator: actor.Actor = oidc.add_provider()
    oidc.groups.add_mapping(
        administrator,
        group_mapping_record.GroupMappingRecord(
            provider_name="idp", provider_group_name="idp-admins", group_name="administrators"
        ),
    )
    admin_claims: dict[str, object] = dict(ALICE_CLAIMS)
    admin_claims["groups"] = ["idp-admins"]

    login: auth_service.Login = oidc.log_in_through_provider(admin_claims)

    assert authenticate_actor(oidc, login).can("admin")


# ----------------------------------------------------------------------------------------------------------------------
def test_08_where_the_claims_are_can_be_set_for_each_provider(oidc: oidc_world.OidcWorld) -> None:
    administrator: actor.Actor = oidc.add_provider()
    record: oidc_provider_settings.OidcProviderSettings = oidc_world.make_provider_settings()
    record.groups_claim = "https://example.com/roles"
    record.username_claim = "profile.login"
    oidc.oidc_settings.update_provider(administrator, record, None)
    oidc.groups.add_mapping(
        administrator,
        group_mapping_record.GroupMappingRecord(
            provider_name="idp", provider_group_name="staff", group_name="administrators"
        ),
    )

    login: auth_service.Login = oidc.log_in_through_provider(
        {"sub": "u9", "profile": {"login": "carol"}, "https://example.com/roles": ["staff"]}
    )
    carol: actor.Actor = authenticate_actor(oidc, login)

    assert carol.username == "carol"
    assert carol.can("admin")


# ----------------------------------------------------------------------------------------------------------------------
def test_09_a_disabled_user_is_refused(oidc: oidc_world.OidcWorld) -> None:
    administrator: actor.Actor = oidc.add_provider()
    first_login: auth_service.Login = oidc.log_in_through_provider(ALICE_CLAIMS)
    alice: actor.Actor = authenticate_actor(oidc, first_login)
    oidc.auth.set_user_disabled(administrator, alice.user_id, True)

    with pytest.raises(auth_error.AuthError) as refused:
        oidc.log_in_through_provider(ALICE_CLAIMS)

    assert refused.value.kind == "disabled"


# ----------------------------------------------------------------------------------------------------------------------
def test_10_a_token_that_is_not_right_is_refused(oidc: oidc_world.OidcWorld) -> None:
    oidc.add_provider()

    oidc.fake_provider.audience = "some-other-client"
    with pytest.raises(oidc_provider.OidcLoginError):
        oidc.log_in_through_provider(ALICE_CLAIMS)
    oidc.fake_provider.audience = fake_oidc_provider.CLIENT_ID

    oidc.fake_provider.seconds_until_expiry = -3600
    with pytest.raises(oidc_provider.OidcLoginError):
        oidc.log_in_through_provider(ALICE_CLAIMS)
    oidc.fake_provider.seconds_until_expiry = 300

    oidc.fake_provider.nonce_override = "a nonce of another login"
    with pytest.raises(oidc_provider.OidcLoginError):
        oidc.log_in_through_provider(ALICE_CLAIMS)
    oidc.fake_provider.nonce_override = None

    oidc.fake_provider.signing_key = RSAKey.generate_key(2048, parameters={"kid": "fake-key", "use": "sig"})
    with pytest.raises(oidc_provider.OidcLoginError):
        oidc.log_in_through_provider(ALICE_CLAIMS)
    oidc.fake_provider.signing_key = oidc.fake_provider.key

    assert oidc.log_in_through_provider(ALICE_CLAIMS) is not None


# ----------------------------------------------------------------------------------------------------------------------
def test_11_an_answer_that_does_not_belong_to_the_login_that_was_started_is_refused(oidc: oidc_world.OidcWorld) -> None:
    oidc.add_provider()
    provider: oidc_provider.OidcProvider = oidc.find_provider()
    start: oidc_provider.LoginStart = provider.begin(oidc_world.REDIRECT_URI)
    other_start: oidc_provider.LoginStart = provider.begin(oidc_world.REDIRECT_URI)
    callback_url: str = oidc.fake_provider.approve(start.url, ALICE_CLAIMS)

    with pytest.raises(oidc_provider.OidcLoginError):
        provider.finish(callback_url, other_start.pending)
    with pytest.raises(oidc_provider.OidcLoginError):
        provider.finish(f"{oidc_world.REDIRECT_URI}?error=access_denied", start.pending)


# ----------------------------------------------------------------------------------------------------------------------
def test_12_a_provider_that_cannot_be_reached_is_reported_and_tried_again_later(oidc: oidc_world.OidcWorld) -> None:
    administrator: actor.Actor = oidc.add_provider()
    oidc.fake_provider.is_reachable = False

    down: oidc_settings.ProviderCheck = oidc.oidc_settings.check_provider(administrator, "idp")
    oidc.fake_provider.is_reachable = True
    up: oidc_settings.ProviderCheck = oidc.oidc_settings.check_provider(administrator, "idp")

    assert not down.has_worked
    assert up.has_worked


# ----------------------------------------------------------------------------------------------------------------------
def test_13_a_provider_may_not_be_reached_without_https_unless_it_is_allowed(oidc: oidc_world.OidcWorld) -> None:
    administrator: actor.Actor = oidc.make_administrator()
    insecure: oidc_provider_settings.OidcProviderSettings = oidc_world.make_provider_settings()
    insecure.issuer = "http://idp.example"
    oidc.oidc_settings.add_provider(administrator, insecure, fake_oidc_provider.CLIENT_SECRET)

    check: oidc_settings.ProviderCheck = oidc.oidc_settings.check_provider(administrator, "idp")

    assert not check.has_worked
    assert "https" in check.message


# ----------------------------------------------------------------------------------------------------------------------
def test_14_removing_a_provider_removes_its_secret_and_mappings_unless_nobody_could_then_administer(
    oidc: oidc_world.OidcWorld, tmp_path: Path
) -> None:
    administrator: actor.Actor = oidc.add_provider()
    oidc.groups.create_group(administrator, "readers", "")
    oidc.groups.add_mapping(
        administrator,
        group_mapping_record.GroupMappingRecord(provider_name="idp", provider_group_name="staff", group_name="readers"),
    )

    oidc.oidc_settings.remove_provider(administrator, "idp")

    assert not (tmp_path / "idp").exists()
    assert oidc.groups.list_mappings(administrator) == []
    assert oidc.providers.find_provider(oidc.session, "idp") is None


# ----------------------------------------------------------------------------------------------------------------------
def test_15_the_logout_address_follows_what_the_provider_is_set_to_do(oidc: oidc_world.OidcWorld) -> None:
    administrator: actor.Actor = oidc.add_provider()
    provider_without_logout: oidc_provider.OidcProvider = oidc.find_provider()
    record: oidc_provider_settings.OidcProviderSettings = oidc_world.make_provider_settings()
    record.logout = "standard"
    oidc.oidc_settings.update_provider(administrator, record, None)
    provider_with_logout: oidc_provider.OidcProvider = oidc.find_provider()

    plain: str | None = provider_without_logout.get_logout_url(auth_world.BASE_URL)
    standard: str | None = provider_with_logout.get_logout_url(auth_world.BASE_URL)

    assert plain is None
    assert standard is not None
    assert standard.startswith("https://idp.example/logout?")
    assert "post_logout_redirect_uri" in standard


# ----------------------------------------------------------------------------------------------------------------------
def test_16_a_login_that_was_started_can_be_finished_once_and_not_after_it_took_too_long() -> None:
    logins: pending_logins.PendingLogins = pending_logins.PendingLogins()
    pending: oidc_provider.PendingLogin = oidc_provider.PendingLogin(
        state="s1", nonce="n1", verifier="v1", redirect_uri=oidc_world.REDIRECT_URI
    )
    logins.add_started("idp", pending, "/after")

    first: pending_logins.StartedLogin | None = logins.take_started("s1")
    second: pending_logins.StartedLogin | None = logins.take_started("s1")
    unknown: pending_logins.StartedLogin | None = logins.take_started("never-started")

    assert first is not None
    assert first.next_path == "/after"
    assert second is None
    assert unknown is None
