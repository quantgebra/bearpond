from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import sqlalchemy

from quantgebra.auth.core import actor
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from quantgebra.auth.core import user_record
from quantgebra.auth.core import group_mapping_record
from quantgebra.auth.local import local_authentication

from . import auth_world
from . import fake_oidc_provider
from . import http_world

ALICE_CLAIMS: dict[str, object] = {
    "sub": "idp-user-1",
    "preferred_username": "alice",
    "name": "Alice Anderson",
    "groups": ["idp-writers"],
}


# ----------------------------------------------------------------------------------------------------------------------
def test_01_the_page_for_logging_in_asks_how_people_can_log_in(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    world.start()
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)

    options = world.client.get("/auth/options").json()

    assert options["mode"] == "enforced"
    assert options["is_local_enabled"]
    assert options["providers"] == [{"provider_name": "idp", "label": "Our identity provider"}]


# ----------------------------------------------------------------------------------------------------------------------
def test_02_what_needs_a_login_is_refused_without_one(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    world.start()

    response = world.client.get("/repos/bearpond")

    assert response.status_code == 401


# ----------------------------------------------------------------------------------------------------------------------
def test_03_following_the_link_logs_the_first_administrator_in(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    outcome = world.start()
    assert outcome.link is not None
    token: str = auth_world.token_of(outcome.link)

    before_choosing = world.client.get(f"/auth/invite/{token}").json()
    chosen = world.client.post(f"/auth/invite/{token}", json={"password": auth_world.STRONG_PASSWORD})
    me = world.client.get("/auth/me").json()
    after_choosing = world.client.get(f"/auth/invite/{token}")

    assert before_choosing["username"] == "admin"
    assert chosen.status_code == 200
    assert me["username"] == "admin"
    assert me["group_names"] == ["administrators"]
    assert after_choosing.status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_04_the_cookie_of_a_login_cannot_be_read_by_scripts(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    outcome = world.start()
    assert outcome.link is not None

    response = world.client.post(
        f"/auth/invite/{auth_world.token_of(outcome.link)}", json={"password": auth_world.STRONG_PASSWORD}
    )
    cookie_header: str = response.headers["set-cookie"].lower()

    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header


# ----------------------------------------------------------------------------------------------------------------------
def test_05_a_request_that_changes_something_needs_the_secret_of_the_login(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    csrf_token: str = world.log_first_administrator_in(world.client)

    without_secret = world.client.post("/repos/bearpond")
    with_wrong_secret = world.client.post("/repos/bearpond", headers={"x-csrf-token": "not the secret"})
    with_secret = world.client.post("/repos/bearpond", headers={"x-csrf-token": csrf_token})
    reading = world.client.get("/repos/bearpond")

    assert without_secret.status_code == 403
    assert with_wrong_secret.status_code == 403
    assert with_secret.status_code == 200
    assert reading.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_06_a_request_from_another_site_is_refused(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    csrf_token: str = world.log_first_administrator_in(world.client)

    from_another_site = world.client.post(
        "/repos/bearpond", headers={"x-csrf-token": csrf_token, "origin": "https://evil.example"}
    )
    from_this_service = world.client.post(
        "/repos/bearpond", headers={"x-csrf-token": csrf_token, "origin": auth_world.BASE_URL}
    )

    assert from_another_site.status_code == 403
    assert from_this_service.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_07_logging_in_with_a_password_and_logging_out(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    world.log_first_administrator_in(world.make_other_client())
    browser = world.make_other_client()

    wrong = browser.post("/auth/login", json={"username": "admin", "password": "not the password at all"})
    right = browser.post("/auth/login", json={"username": "admin", "password": auth_world.STRONG_PASSWORD})
    logged_in = browser.get("/auth/me").status_code
    logout = browser.post("/auth/logout")
    logged_out = browser.get("/auth/me").status_code

    assert wrong.status_code == 401
    assert right.status_code == 200
    assert logged_in == 200
    assert logout.json() == {"redirect_url": None}
    assert logged_out == 401


# ----------------------------------------------------------------------------------------------------------------------
def test_08_a_permission_can_be_needed_for_the_repository_a_request_is_about(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    world.log_first_administrator_in(world.make_other_client())
    with world.open_services() as services:
        administrator: actor.Actor = services.auth.authenticate(
            services.local.login("admin", auth_world.STRONG_PASSWORD, "10.0.0.1").token
        ).actor  # type: ignore[union-attr]
        services.groups.create_group(administrator, "bearpond-team", "")
        services.settings.replace_rules(administrator, [auth_world.make_rules()[2], _make_team_rule()])
        request: local_authentication.InviteRequest = local_authentication.InviteRequest(
            username="alice", display_name="Alice", group_names=["bearpond-team"]
        )
        link: local_authentication.InviteLink = services.local.invite(administrator, request)
    alice_browser = world.make_other_client()
    accepted = alice_browser.post(
        f"/auth/invite/{auth_world.token_of(link)}", json={"password": auth_world.STRONG_PASSWORD}
    )
    headers: dict[str, str] = {"x-csrf-token": accepted.json()["csrf_token"]}

    on_own_repository = alice_browser.post("/repos/bearpond", headers=headers)
    on_other_repository = alice_browser.post("/repos/bullpen", headers=headers)
    reading_other_repository = alice_browser.get("/repos/bullpen")

    assert on_own_repository.status_code == 200
    assert on_other_repository.status_code == 403
    assert reading_other_repository.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def _make_team_rule() -> grant_rule.GrantRule:
    return grant_rule.GrantRule(
        group_name="bearpond-team",
        grants=[grant.Grant(permission="write", subject="bearpond", subject_kind="repository")],
    )


# ----------------------------------------------------------------------------------------------------------------------
def test_09_logging_in_through_a_provider_sends_the_person_on_to_where_they_were_going(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)
    browser = world.make_other_client()

    sent_on_to: str = world.finish_login_at_provider(browser, ALICE_CLAIMS, "/repos/bearpond")
    me = browser.get("/auth/me").json()

    assert sent_on_to == "/repos/bearpond"
    assert me["username"] == "alice"
    assert me["issuer"] == fake_oidc_provider.ISSUER


# ----------------------------------------------------------------------------------------------------------------------
def test_10_a_person_is_never_sent_on_to_another_site(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)

    sent_on_to: str = world.finish_login_at_provider(world.make_other_client(), ALICE_CLAIMS, "//evil.example/steal")

    assert sent_on_to == "/"


# ----------------------------------------------------------------------------------------------------------------------
def test_11_an_answer_from_a_provider_that_this_service_did_not_ask_for_is_refused(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)

    response = world.client.get("/auth/oidc/idp/callback?code=abc&state=never-started")
    unknown_provider = world.client.get("/auth/oidc/nobody/login")

    assert response.status_code == 400
    assert unknown_provider.status_code == 404
    assert "set-cookie" not in response.headers


# ----------------------------------------------------------------------------------------------------------------------
def test_12_a_mapped_provider_group_gives_what_the_rules_give(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)
    with world.open_services() as services:
        services.groups.add_mapping(
            world.get_actor(services, administrator_client),
            group_mapping_record.GroupMappingRecord(
                provider_name="idp", provider_group_name="idp-writers", group_name="writers"
            ),
        )
    browser = world.make_other_client()
    world.finish_login_at_provider(browser, ALICE_CLAIMS)
    csrf_token: str = browser.get("/auth/me").json()["csrf_token"]

    writing = browser.post("/repos/bearpond", headers={"x-csrf-token": csrf_token})

    assert writing.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_13_in_development_mode_nobody_logs_in_and_everybody_may_do_everything(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path, "none")

    me = world.client.get("/auth/me").json()
    writing = world.client.post("/repos/bearpond")

    assert me["username"] == "developer"
    assert writing.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_14_changing_the_password_over_http(engine: sqlalchemy.Engine, tmp_path: Path) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path)
    csrf_token: str = world.log_first_administrator_in(world.client)
    headers: dict[str, str] = {"x-csrf-token": csrf_token}

    wrong = world.client.post(
        "/auth/password",
        headers=headers,
        json={"current_password": "wrong", "new_password": "a brand new long password"},
    )
    right = world.client.post(
        "/auth/password",
        headers=headers,
        json={"current_password": auth_world.STRONG_PASSWORD, "new_password": "a brand new long password"},
    )
    relogin = world.make_other_client().post(
        "/auth/login", json={"username": "admin", "password": "a brand new long password"}
    )

    assert wrong.status_code == 401
    assert right.status_code == 204
    assert relogin.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_15_a_service_with_a_login_page_gets_back_the_person_whose_login_did_not_work(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path, "enforced", "/login")
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)
    browser = world.make_other_client()

    # the person says no at the provider, which sends them back with an error instead of a code
    started: httpx.Response = browser.get("/auth/oidc/idp/login?next=/repos/bearpond")
    state: str = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    cancelled: httpx.Response = browser.get(f"/auth/oidc/idp/callback?error=access_denied&state={state}")
    never_started: httpx.Response = browser.get("/auth/oidc/idp/callback?code=abc&state=never-started")
    unknown_provider: httpx.Response = browser.get("/auth/oidc/nobody/login?next=/repos/bearpond")

    assert cancelled.status_code == 303
    assert cancelled.headers["location"] == "/login?error=cancelled&next=/repos/bearpond"
    assert never_started.headers["location"] == "/login?error=expired&next=/"
    assert unknown_provider.headers["location"] == "/login?error=unknown_provider&next=/repos/bearpond"
    assert "set-cookie" not in cancelled.headers


# ----------------------------------------------------------------------------------------------------------------------
def test_16_a_disabled_account_is_turned_away_at_the_callback_with_nothing_kept(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    world: http_world.HttpWorld = http_world.HttpWorld(engine, tmp_path, "enforced", "/login")
    administrator_client = world.make_other_client()
    world.log_first_administrator_in(administrator_client)
    world.add_provider(administrator_client)
    world.finish_login_at_provider(world.make_other_client(), ALICE_CLAIMS)
    with world.open_services() as services:
        administrator: actor.Actor = world.get_actor(services, administrator_client)
        alice: user_record.UserRecord | None = user_record.UserRecord.get_by_issuer_and_username(
            services.session, fake_oidc_provider.ISSUER, "alice"
        )
        assert alice is not None
        services.auth.set_user_disabled(administrator, alice.user_id, True)
    browser = world.make_other_client()

    started: httpx.Response = browser.get("/auth/oidc/idp/login?next=/repos/bearpond")
    callback_url: str = world.fake_provider.approve(started.headers["location"], ALICE_CLAIMS)
    turned_away: httpx.Response = browser.get(f"/auth/oidc/idp/callback?{urlparse(callback_url).query}")

    assert turned_away.status_code == 303
    assert turned_away.headers["location"] == "/login?error=disabled&next=/repos/bearpond"
    assert "set-cookie" not in turned_away.headers
