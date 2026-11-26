from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import orm
from quantgebra.auth import auth_services
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from quantgebra.auth.local import local_authentication

import conftest
from bearpond.protocol import types
from bearpond.server import config
from bearpond.server import server as server_module
from bearpond.server import server_auth

STATIC_TOKEN = "s3cret"
STRONG_PASSWORD = "correct horse battery staple"
HIVE_PATH = "year=2024/month=01/part.parquet"
CONTENT = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def enforced_url(repo_root: Path) -> Iterator[str]:
    # a server that makes people log in, with two repositories, and the shared token as well; the first administrator
    # is made at startup, as it is for a server that is run for real
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
            auth=config.AuthSettings(
                mode="enforced",
                secrets_dir=repo_root / "auth-secrets",
            ),
            static_token=STATIC_TOKEN,
        )
    )
    with orm.Session(srv.engine, expire_on_commit=False) as session:
        srv.create_repository(session, "alpha")
        srv.create_repository(session, "beta")
        session.commit()
    with conftest.running_server() as url:
        yield url
    server_module.close()


# ----------------------------------------------------------------------------------------------------------------------
def make_administrator_login() -> auth_service.Login:
    # the first administrator follows the link that the server made at its start, and logs in with the password they
    # chose when they come again
    login: auth_service.Login
    with server_module.get_server().auth.open_services() as services:
        outcome: local_authentication.FirstAdministratorOutcome = services.local.ensure_first_administrator()
        if outcome.link is not None:
            login = services.local.accept_invite(outcome.link.url.rsplit("/", 1)[1], STRONG_PASSWORD)
        else:
            login = services.local.login("admin", STRONG_PASSWORD, "10.0.0.1")
    return login


# ----------------------------------------------------------------------------------------------------------------------
def make_login(username: str, group_names: list[str]) -> auth_service.Login:
    # an account that an administrator invited, whose owner has chosen a password
    administrator_login: auth_service.Login = make_administrator_login()
    login: auth_service.Login
    with server_module.get_server().auth.open_services() as services:
        administrator: actor.Actor = authenticate(services, administrator_login)
        request: local_authentication.InviteRequest = local_authentication.InviteRequest(
            username=username, display_name=username.title(), group_names=group_names
        )
        link: local_authentication.InviteLink = services.local.invite(administrator, request)
        login = services.local.accept_invite(link.url.rsplit("/", 1)[1], STRONG_PASSWORD)
    return login


# ----------------------------------------------------------------------------------------------------------------------
def authenticate(services: auth_services.AuthServices, login: auth_service.Login) -> actor.Actor:
    authenticated: auth_service.AuthenticatedRequest | None = services.auth.authenticate(login.token)
    assert authenticated is not None
    return authenticated.actor


# ----------------------------------------------------------------------------------------------------------------------
def make_browser(url: str, login: auth_service.Login) -> httpx.Client:
    # a client that carries the cookie of a login, and the secret that what changes something has to send
    return httpx.Client(
        base_url=url,
        cookies={server_auth.SESSION_COOKIE_NAME: login.token},
        headers={"x-csrf-token": login.session.csrf_token},
        timeout=30.0,
    )


# ----------------------------------------------------------------------------------------------------------------------
def test_01_a_server_that_enforces_logins_refuses_anonymous_requests_except_the_version(enforced_url: str) -> None:
    with httpx.Client(base_url=enforced_url, timeout=30.0) as anonymous:
        listing: httpx.Response = anonymous.get("/repos")
        manifest: httpx.Response = anonymous.get("/repos/alpha/manifest")
        version: httpx.Response = anonymous.get("/version")

    assert listing.status_code == 401
    assert manifest.status_code == 401
    assert version.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_first_administrator_logs_in_with_the_link_and_sees_every_repository(enforced_url: str) -> None:
    login: auth_service.Login = make_administrator_login()

    with make_browser(enforced_url, login) as browser:
        listing: httpx.Response = browser.get("/repos")

    assert [info["name"] for info in listing.json()] == ["alpha", "beta"]


# ----------------------------------------------------------------------------------------------------------------------
def test_03_only_an_administrator_makes_a_repository(enforced_url: str) -> None:
    administrator_login: auth_service.Login = make_administrator_login()
    writer_login: auth_service.Login = make_login("wendy", ["writers"])

    with make_browser(enforced_url, administrator_login) as administrator_browser:
        made: httpx.Response = administrator_browser.post("/repos", json={"name": "gamma"})
    with make_browser(enforced_url, writer_login) as writer_browser:
        refused: httpx.Response = writer_browser.post("/repos", json={"name": "delta"})

    assert made.status_code == 201
    assert refused.status_code == 403


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_request_that_changes_something_needs_the_secret_of_the_login(enforced_url: str) -> None:
    administrator_login: auth_service.Login = make_administrator_login()

    with httpx.Client(
        base_url=enforced_url, cookies={server_auth.SESSION_COOKIE_NAME: administrator_login.token}, timeout=30.0
    ) as without_secret:
        refused: httpx.Response = without_secret.post("/repos", json={"name": "gamma"})
        reading: httpx.Response = without_secret.get("/repos")

    assert refused.status_code == 403
    assert reading.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_05_a_reader_reads_but_does_not_write_and_a_writer_does_both(enforced_url: str) -> None:
    reader_login: auth_service.Login = make_login("rita", ["viewers"])
    writer_login: auth_service.Login = make_login("wendy", ["writers"])
    begin: dict = {"added": [conftest.file_meta(HIVE_PATH, CONTENT).model_dump()], "removed": []}

    with make_browser(enforced_url, reader_login) as reader:
        reader_reads: httpx.Response = reader.get("/repos/alpha/commits")
        reader_writes: httpx.Response = reader.post("/repos/alpha/transactions", json=begin)
    with make_browser(enforced_url, writer_login) as writer:
        writer_writes: httpx.Response = writer.post("/repos/alpha/transactions", json=begin)

    assert reader_reads.status_code == 200
    assert reader_writes.status_code == 403
    assert writer_writes.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_06_a_permission_can_be_limited_to_one_repository_and_the_listing_follows_it(enforced_url: str) -> None:
    administrator_login: auth_service.Login = make_administrator_login()
    with server_module.get_server().auth.open_services() as services:
        administrator: actor.Actor = authenticate(services, administrator_login)
        services.groups.create_group(administrator, "alpha-team", "")
        services.settings.replace_rules(
            administrator,
            [
                *server_auth.ServerAuth.make_default_rules(),
                grant_rule.GrantRule(
                    group_name="alpha-team",
                    grants=[grant.Grant(permission="read", subject="alpha", subject_kind="repository")],
                ),
            ],
        )
    login: auth_service.Login = make_login("alice", ["alpha-team"])

    with make_browser(enforced_url, login) as browser:
        listing: httpx.Response = browser.get("/repos")
        alpha: httpx.Response = browser.get("/repos/alpha/commits")
        beta: httpx.Response = browser.get("/repos/beta/commits")

    assert [info["name"] for info in listing.json()] == ["alpha"]
    assert alpha.status_code == 200
    assert beta.status_code == 403


# ----------------------------------------------------------------------------------------------------------------------
def test_07_the_shared_token_still_works_as_an_administrator_and_a_wrong_one_does_not(enforced_url: str) -> None:
    with httpx.Client(base_url=enforced_url, headers={"authorization": f"Bearer {STATIC_TOKEN}"}) as scripts:
        listing: httpx.Response = scripts.get("/repos")
        made: httpx.Response = scripts.post("/repos", json={"name": "gamma"})
    with httpx.Client(base_url=enforced_url, headers={"authorization": "Bearer wrong"}) as impostor:
        refused: httpx.Response = impostor.get("/repos")

    assert listing.status_code == 200
    assert made.status_code == 201
    assert refused.status_code == 401


# ----------------------------------------------------------------------------------------------------------------------
def test_08_a_person_who_is_logged_in_is_the_author_of_what_they_commit(enforced_url: str) -> None:
    login: auth_service.Login = make_login("wendy", ["writers"])
    begin: dict = {
        "added": [conftest.file_meta(HIVE_PATH, CONTENT).model_dump()],
        "removed": [],
        "user": "somebody-else",
    }

    with make_browser(enforced_url, login) as browser:
        txn_uuid: str = browser.post("/repos/alpha/transactions", json=begin).json()["txn_uuid"]
        browser.put(f"/repos/alpha/transactions/{txn_uuid}/files/{HIVE_PATH}", content=CONTENT)
        committed: httpx.Response = browser.post(f"/repos/alpha/transactions/{txn_uuid}/commit")
        history_response: httpx.Response = browser.get("/repos/alpha/commits")
        history: types.CommitHistoryPage = types.CommitHistoryPage.model_validate(history_response.json())

    assert committed.status_code == 200
    assert [record.user for record in history.records] == ["wendy"]


# ----------------------------------------------------------------------------------------------------------------------
def test_09_the_login_endpoints_are_part_of_the_server(enforced_url: str) -> None:
    make_administrator_login()

    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        options: dict = browser.get("/auth/options").json()
        wrong: httpx.Response = browser.post("/auth/login", json={"username": "admin", "password": "not the password"})
        right: httpx.Response = browser.post("/auth/login", json={"username": "admin", "password": STRONG_PASSWORD})
        me: dict = browser.get("/auth/me").json()

    assert options["mode"] == "enforced"
    assert wrong.status_code == 401
    assert right.status_code == 200
    assert me["username"] == "admin"
