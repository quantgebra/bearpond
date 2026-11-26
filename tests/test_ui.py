import re
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import orm
from quantgebra.auth.local import local_authentication

import conftest
from bearpond.server import config
from bearpond.server import repository
from bearpond.server import server as server_module
from bearpond.server import server_auth

STRONG_PASSWORD: str = "correct horse battery staple"
OTHER_PASSWORD: str = "tremendous purple elephant"
HIVE_PATH: str = "year=2024/month=01/part.parquet"
CONTENT: bytes = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def ui(live_server: str) -> Iterator[httpx.Client]:
    # a browser on a server that lets everyone in, as it is run on one's own machine
    with httpx.Client(base_url=live_server, timeout=30.0) as browser:
        yield browser


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def enforced_url(repo_root: Path) -> Iterator[str]:
    # a server that makes people log in, with two repositories, one of which has a file in it
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
            auth=config.AuthSettings(mode="enforced", secrets_dir=repo_root / "auth-secrets"),
        )
    )
    with orm.Session(srv.engine, expire_on_commit=False) as session:
        alpha_repo: repository.Repository = srv.create_repository(session, "alpha")
        srv.create_repository(session, "beta")
        session.commit()
        conftest.commit_files(alpha_repo, {HIVE_PATH: CONTENT})
    with conftest.running_server() as url:
        yield url
    server_module.close()


# ----------------------------------------------------------------------------------------------------------------------
def make_invite_path(username: str, group_names: list[str]) -> str:
    # the path of the link that an administrator sends a new person, or that the server makes for the first
    # administrator when nobody can log in yet
    link: local_authentication.InviteLink
    with server_module.get_server().auth.open_services() as services:
        if username == "admin":
            outcome: local_authentication.FirstAdministratorOutcome = services.local.ensure_first_administrator()
            assert outcome.link is not None
            link = outcome.link
        else:
            request: local_authentication.InviteRequest = local_authentication.InviteRequest(
                username=username, display_name=username.title(), group_names=group_names
            )
            link = services.local.invite(None, request)
    return "/invite/" + link.url.rsplit("/", 1)[1]


# ----------------------------------------------------------------------------------------------------------------------
def find_csrf_token(html: str) -> str:
    # the secret that the forms of a page carry
    match: re.Match[str] | None = re.search(r'name="_csrf" value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


# ----------------------------------------------------------------------------------------------------------------------
def test_01_the_home_page_is_the_list_of_repositories(ui: httpx.Client) -> None:
    home: httpx.Response = ui.get("/")
    listing: httpx.Response = ui.get("/repositories")

    assert home.status_code == 303
    assert home.headers["location"] == "/repositories"
    assert listing.status_code == 200
    assert "text/html" in listing.headers["content-type"]
    assert ">testrepo</a>" in listing.text
    assert "development mode" in listing.text


# ----------------------------------------------------------------------------------------------------------------------
def test_02_a_repository_page_browses_its_directories_and_shows_its_latest_commits(
    ui: httpx.Client, repo: repository.Repository
) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT, "README.md": b"hello"})

    root_page: httpx.Response = ui.get("/repositories/testrepo")
    month_page: httpx.Response = ui.get("/repositories/testrepo", params={"path": "year=2024/month=01"})

    assert "year=2024/" in root_page.text
    assert "README.md" in root_page.text
    assert "part.parquet" not in root_page.text
    assert "#1" in root_page.text
    assert "part.parquet" in month_page.text
    assert f'href="/repos/testrepo/files/{HIVE_PATH}"' in month_page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_03_a_commit_page_shows_what_the_commit_changed(ui: httpx.Client, repo: repository.Repository) -> None:
    conftest.commit_files(repo, {HIVE_PATH: CONTENT})
    conftest.remove_files(repo, {HIVE_PATH: CONTENT})

    history: httpx.Response = ui.get("/repositories/testrepo/commits")
    removal: httpx.Response = ui.get("/repositories/testrepo/commits/2")

    assert 'href="/repositories/testrepo/commits/1"' in history.text
    assert 'href="/repositories/testrepo/commits/2"' in history.text
    assert removal.status_code == 200
    assert "Removed" in removal.text
    assert HIVE_PATH in removal.text


# ----------------------------------------------------------------------------------------------------------------------
def test_04_what_is_not_there_is_a_page_that_says_so(ui: httpx.Client) -> None:
    no_repository: httpx.Response = ui.get("/repositories/nope")
    no_commit: httpx.Response = ui.get("/repositories/testrepo/commits/9")

    assert no_repository.status_code == 404
    assert "There is no repository named nope." in no_repository.text
    assert no_commit.status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_05_names_are_escaped_so_they_are_never_taken_for_markup(
    ui: httpx.Client, repo: repository.Repository
) -> None:
    conftest.commit_files(repo, {"<em>x.csv": b"x"})

    page: httpx.Response = ui.get("/repositories/testrepo")

    assert "<em>x.csv" not in page.text
    assert "&lt;em&gt;x.csv" in page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_06_the_stylesheet_and_script_are_served_as_they_are(ui: httpx.Client) -> None:
    stylesheet: httpx.Response = ui.get("/static/style.css")
    script: httpx.Response = ui.get("/static/live.js")

    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]
    assert script.status_code == 200


# ----------------------------------------------------------------------------------------------------------------------
def test_07_when_everyone_is_let_in_the_login_page_sends_people_on(ui: httpx.Client) -> None:
    login_page: httpx.Response = ui.get("/login", params={"next": "/repositories/testrepo"})

    assert login_page.status_code == 303
    assert login_page.headers["location"] == "/repositories/testrepo"


# ----------------------------------------------------------------------------------------------------------------------
def test_08_a_server_that_takes_only_the_shared_token_says_how_to_log_in_from_a_browser(
    reconfigure: Callable[[str | None, config.AuthSettings | None], None], live_server: str
) -> None:
    reconfigure("s3cret", None)

    with httpx.Client(base_url=live_server, timeout=30.0) as browser:
        page: httpx.Response = browser.get("/repositories")

    assert page.status_code == 403
    assert "auth.mode to enforced" in page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_09_a_person_who_is_not_logged_in_is_sent_to_log_in(enforced_url: str) -> None:
    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        page: httpx.Response = browser.get("/repositories/alpha", params={"path": "year=2024"})
        login_page: httpx.Response = browser.get(page.headers["location"])

    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=/repositories/alpha%3Fpath%3Dyear%253D2024"
    assert login_page.status_code == 200
    assert 'name="next" value="/repositories/alpha?path=year%3D2024"' in login_page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_10_the_first_administrator_chooses_a_password_with_the_link_and_is_logged_in(enforced_url: str) -> None:
    invite_path: str = make_invite_path("admin", [])

    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        invite_page: httpx.Response = browser.get(invite_path)
        mismatch: httpx.Response = browser.post(
            invite_path, data={"password": STRONG_PASSWORD, "password_again": OTHER_PASSWORD}
        )
        accepted: httpx.Response = browser.post(
            invite_path, data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD}
        )
        listing: httpx.Response = browser.get("/repositories")
        used_link: httpx.Response = browser.get(invite_path)

    assert "for the account <code>admin</code>" in invite_page.text
    assert "The two passwords are not the same." in mismatch.text
    assert accepted.status_code == 303
    assert server_auth.SESSION_COOKIE_NAME in accepted.cookies
    assert ">alpha</a>" in listing.text
    assert ">beta</a>" in listing.text
    assert "administrator" in listing.text
    assert used_link.status_code == 404


# ----------------------------------------------------------------------------------------------------------------------
def test_11_a_person_logs_in_with_the_form_and_lands_where_they_were_going(enforced_url: str) -> None:
    with httpx.Client(base_url=enforced_url, timeout=30.0) as first_browser:
        first_browser.post(
            make_invite_path("admin", []), data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD}
        )

    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        wrong: httpx.Response = browser.post(
            "/login", data={"username": "admin", "password": OTHER_PASSWORD, "next": "/repositories/alpha"}
        )
        right: httpx.Response = browser.post(
            "/login", data={"username": "admin", "password": STRONG_PASSWORD, "next": "/repositories/alpha"}
        )
        repository_page: httpx.Response = browser.get(right.headers["location"])

    assert wrong.status_code == 401
    assert 'value="admin"' in wrong.text
    assert right.status_code == 303
    assert right.headers["location"] == "/repositories/alpha"
    assert "year=2024/" in repository_page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_12_logging_out_needs_the_secret_of_the_page_and_ends_the_login(enforced_url: str) -> None:
    invite_path: str = make_invite_path("admin", [])

    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        browser.post(invite_path, data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD})
        page: httpx.Response = browser.get("/repositories")
        forged: httpx.Response = browser.post("/logout", data={"_csrf": "forged"})
        logged_out: httpx.Response = browser.post("/logout", data={"_csrf": find_csrf_token(page.text)})
        afterwards: httpx.Response = browser.get("/repositories")

    assert forged.status_code == 403
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/login"
    assert afterwards.status_code == 303


# ----------------------------------------------------------------------------------------------------------------------
def test_13_a_person_changes_their_password_on_the_account_page(enforced_url: str) -> None:
    invite_path: str = make_invite_path("viv", ["viewers"])

    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        browser.post(invite_path, data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD})
        account: httpx.Response = browser.get("/account")
        changed: httpx.Response = browser.post(
            "/account/password",
            data={
                "_csrf": find_csrf_token(account.text),
                "current_password": STRONG_PASSWORD,
                "new_password": OTHER_PASSWORD,
                "new_password_again": OTHER_PASSWORD,
            },
        )

    with httpx.Client(base_url=enforced_url, timeout=30.0) as other_browser:
        login: httpx.Response = other_browser.post("/login", data={"username": "viv", "password": OTHER_PASSWORD})

    assert "viewers" in account.text
    assert "Your password has been changed." in changed.text
    assert login.status_code == 303


# ----------------------------------------------------------------------------------------------------------------------
def test_14_a_person_in_no_group_is_told_to_ask_for_one(enforced_url: str) -> None:
    invite_path: str = make_invite_path("nobody", [])

    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        browser.post(invite_path, data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD})
        page: httpx.Response = browser.get("/repositories")

    assert page.status_code == 403
    assert "Ask an administrator to put you in a group." in page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_15_a_login_page_offers_the_form_for_accounts_of_this_server(enforced_url: str) -> None:
    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        login_page: httpx.Response = browser.get("/login")

    assert 'name="username"' in login_page.text
    assert 'name="password"' in login_page.text
