import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import orm
from quantgebra.auth.local import local_authentication

import conftest
from bearpond.server import config
from bearpond.server import repository
from bearpond.server import server as server_module

STRONG_PASSWORD: str = "correct horse battery staple"
HIVE_PATH: str = "year=2024/month=01/part.parquet"
CONTENT: bytes = b"fake parquet bytes"


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
@pytest.fixture
def admin(enforced_url: str) -> Iterator[httpx.Client]:
    # the first administrator, who has chosen a password with the link that the server made and is logged in
    with server_module.get_server().auth.open_services() as services:
        outcome: local_authentication.FirstAdministratorOutcome = services.local.ensure_first_administrator()
    assert outcome.link is not None
    with httpx.Client(base_url=enforced_url, timeout=30.0) as browser:
        accept_invite(browser, outcome.link.url)
        yield browser


# ----------------------------------------------------------------------------------------------------------------------
def accept_invite(browser: httpx.Client, link_url: str) -> httpx.Response:
    # the person that a link was given to chooses a password with it, and is logged in
    invite_path: str = "/invite/" + link_url.rsplit("/", 1)[1]
    response: httpx.Response = browser.post(
        invite_path, data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD}
    )
    assert response.status_code == 303
    return response


# ----------------------------------------------------------------------------------------------------------------------
def find_csrf_token(html: str) -> str:
    match: re.Match[str] | None = re.search(r'name="_csrf" value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


# ----------------------------------------------------------------------------------------------------------------------
def find_link_url(html: str) -> str:
    # the link that the page shows once, after an invitation or a reset
    match: re.Match[str] | None = re.search(r'class="copy-field" value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


# ----------------------------------------------------------------------------------------------------------------------
def find_user_id(html: str, username: str) -> str:
    # the id in the address of the forms of a user's row
    rows: list[str] = html.split("<tr>")
    user_id: str = ""
    for row in rows:
        if f"<code>{username}</code>" in row:
            match: re.Match[str] | None = re.search(r'action="/admin/users/([^/"]+)/groups"', row)
            assert match is not None
            user_id = match.group(1)
    assert user_id
    return user_id


# ----------------------------------------------------------------------------------------------------------------------
def post_form(
    browser: httpx.Client, page_path: str, action_path: str, data: dict[str, str | list[str]]
) -> httpx.Response:
    # submits a form of a page the way a browser does: with the secret that the page carries
    page: httpx.Response = browser.get(page_path)
    form_data: dict[str, str | list[str]] = {"_csrf": find_csrf_token(page.text)}
    form_data.update(data)
    return browser.post(action_path, data=form_data)


# ----------------------------------------------------------------------------------------------------------------------
def invite(browser: httpx.Client, username: str, group_names: list[str]) -> str:
    # an administrator invites someone on the users page; the url of the link that the page shows is returned
    response: httpx.Response = post_form(
        browser, "/admin/users", "/admin/users/invite", {"username": username, "groups": group_names}
    )
    assert response.status_code == 200
    return find_link_url(response.text)


# ----------------------------------------------------------------------------------------------------------------------
def test_01_only_administrators_see_the_admin_pages(admin: httpx.Client, enforced_url: str) -> None:
    link_url: str = invite(admin, "viv", ["viewers"])

    with httpx.Client(base_url=enforced_url, timeout=30.0) as viewer:
        accept_invite(viewer, link_url)
        viewer_listing: httpx.Response = viewer.get("/repositories")
        viewer_users: httpx.Response = viewer.get("/admin/users")
    admin_listing: httpx.Response = admin.get("/repositories")

    assert 'href="/admin/users"' in admin_listing.text
    assert 'href="/admin/users"' not in viewer_listing.text
    assert viewer_users.status_code == 403


# ----------------------------------------------------------------------------------------------------------------------
def test_02_an_invitation_link_is_shown_once_and_makes_an_account_in_the_groups_chosen(
    admin: httpx.Client, enforced_url: str
) -> None:
    link_url: str = invite(admin, "wendy", ["writers"])
    users_before: httpx.Response = admin.get("/admin/users")

    with httpx.Client(base_url=enforced_url, timeout=30.0) as writer:
        accept_invite(writer, link_url)
        account: httpx.Response = writer.get("/account")
    users_after: httpx.Response = admin.get("/admin/users")

    assert link_url.startswith("http://127.0.0.1:8000/invite/")
    assert link_url not in users_before.text
    assert "invited" in users_before.text
    assert "writers" in account.text
    assert users_after.text.count("<i class=\"dot dot-green\"></i>active") == 2


# ----------------------------------------------------------------------------------------------------------------------
def test_03_an_administrator_changes_the_groups_of_a_user(admin: httpx.Client, enforced_url: str) -> None:
    link_url: str = invite(admin, "viv", [])
    with httpx.Client(base_url=enforced_url, timeout=30.0) as person:
        accept_invite(person, link_url)
        before: httpx.Response = person.get("/repositories")

        user_id: str = find_user_id(admin.get("/admin/users").text, "viv")
        changed: httpx.Response = post_form(
            admin, "/admin/users", f"/admin/users/{user_id}/groups", {"groups": ["viewers"]}
        )
        unknown: httpx.Response = post_form(
            admin, "/admin/users", f"/admin/users/{user_id}/groups", {"groups": ["nonexistent"]}
        )
        after: httpx.Response = person.get("/repositories")

    assert before.status_code == 403
    assert changed.status_code == 303
    assert unknown.status_code == 400
    assert "There is no group named nonexistent" in unknown.text
    assert after.status_code == 200
    assert ">alpha</a>" in after.text


# ----------------------------------------------------------------------------------------------------------------------
def test_04_a_user_who_is_turned_off_is_logged_out_and_cannot_log_in(admin: httpx.Client, enforced_url: str) -> None:
    link_url: str = invite(admin, "viv", ["viewers"])
    with httpx.Client(base_url=enforced_url, timeout=30.0) as person:
        accept_invite(person, link_url)
        user_id: str = find_user_id(admin.get("/admin/users").text, "viv")
        post_form(admin, "/admin/users", f"/admin/users/{user_id}/disable", {})
        while_off: httpx.Response = person.get("/repositories")
        login_while_off: httpx.Response = person.post("/login", data={"username": "viv", "password": STRONG_PASSWORD})
        post_form(admin, "/admin/users", f"/admin/users/{user_id}/enable", {})
        login_after: httpx.Response = person.post("/login", data={"username": "viv", "password": STRONG_PASSWORD})

    assert while_off.status_code == 303
    assert login_while_off.status_code == 401
    assert login_after.status_code == 303


# ----------------------------------------------------------------------------------------------------------------------
def test_05_an_administrator_cannot_turn_their_own_account_off(admin: httpx.Client) -> None:
    users: httpx.Response = admin.get("/admin/users")
    admin_id: str = find_user_id(users.text, "admin")

    refused: httpx.Response = post_form(admin, "/admin/users", f"/admin/users/{admin_id}/disable", {})

    assert f'action="/admin/users/{admin_id}/disable"' not in users.text
    assert refused.status_code == 400
    assert "You cannot disable your own account." in refused.text


# ----------------------------------------------------------------------------------------------------------------------
def test_06_a_new_password_link_lets_the_person_choose_another_password(admin: httpx.Client, enforced_url: str) -> None:
    link_url: str = invite(admin, "viv", ["viewers"])
    with httpx.Client(base_url=enforced_url, timeout=30.0) as person:
        accept_invite(person, link_url)

    user_id: str = find_user_id(admin.get("/admin/users").text, "viv")
    reset: httpx.Response = post_form(admin, "/admin/users", f"/admin/users/{user_id}/reset", {})
    reset_url: str = find_link_url(reset.text)
    with httpx.Client(base_url=enforced_url, timeout=30.0) as person_again:
        reset_page: httpx.Response = person_again.get("/invite/" + reset_url.rsplit("/", 1)[1])

    assert "Choose a new password" in reset_page.text


# ----------------------------------------------------------------------------------------------------------------------
def test_07_groups_are_added_described_and_removed_when_nothing_depends_on_them(admin: httpx.Client) -> None:
    added: httpx.Response = post_form(
        admin, "/admin/groups", "/admin/groups", {"group_name": "research", "description": "the quants"}
    )
    duplicate: httpx.Response = post_form(admin, "/admin/groups", "/admin/groups", {"group_name": "research"})
    described: httpx.Response = post_form(
        admin, "/admin/groups", "/admin/groups/research/describe", {"description": "the quant researchers"}
    )
    listed: httpx.Response = admin.get("/admin/groups")
    removed: httpx.Response = post_form(admin, "/admin/groups", "/admin/groups/research/delete", {})
    administrators: httpx.Response = post_form(admin, "/admin/groups", "/admin/groups/administrators/delete", {})

    assert added.status_code == 303
    assert duplicate.status_code == 400
    assert described.status_code == 303
    assert 'value="the quant researchers"' in listed.text
    assert removed.status_code == 303
    assert "the quant researchers" not in admin.get("/admin/groups").text
    assert administrators.status_code == 400


# ----------------------------------------------------------------------------------------------------------------------
def test_08_a_rule_gives_a_group_one_repository(admin: httpx.Client, enforced_url: str) -> None:
    post_form(admin, "/admin/groups", "/admin/groups", {"group_name": "research"})
    added: httpx.Response = post_form(
        admin,
        "/admin/access",
        "/admin/access/rules",
        {"group_name": "research", "permission_name": "read", "repo_name": "alpha"},
    )
    again: httpx.Response = post_form(
        admin,
        "/admin/access",
        "/admin/access/rules",
        {"group_name": "research", "permission_name": "read", "repo_name": "alpha"},
    )
    link_url: str = invite(admin, "rita", ["research"])

    with httpx.Client(base_url=enforced_url, timeout=30.0) as researcher:
        accept_invite(researcher, link_url)
        listing: httpx.Response = researcher.get("/repositories")
        alpha_page: httpx.Response = researcher.get("/repositories/alpha")
        beta_page: httpx.Response = researcher.get("/repositories/beta")

    assert added.status_code == 303
    assert again.status_code == 400
    assert ">alpha</a>" in listing.text
    assert ">beta</a>" not in listing.text
    assert alpha_page.status_code == 200
    assert beta_page.status_code == 403


# ----------------------------------------------------------------------------------------------------------------------
def test_09_a_rule_is_removed_unless_that_would_lock_everyone_out(admin: httpx.Client) -> None:
    access: httpx.Response = admin.get("/admin/access")
    rows: list[str] = []
    for chunk in access.text.split("<tr>"):
        rows.append(chunk.split("</tr>")[0])
    administrators_indexes: re.Match[str] | None = None
    viewers_indexes: re.Match[str] | None = None
    index_pattern: str = r'name="rule_index" value="(\d+)">\s*<input type="hidden" name="grant_index" value="(\d+)"'
    for row in rows:
        if "administrators" in row:
            administrators_indexes = re.search(index_pattern, row)
        if ">viewers<" in row:
            viewers_indexes = re.search(index_pattern, row)
    assert administrators_indexes is not None
    assert viewers_indexes is not None

    locked_out: httpx.Response = post_form(
        admin,
        "/admin/access",
        "/admin/access/rules/delete",
        {"rule_index": administrators_indexes.group(1), "grant_index": administrators_indexes.group(2)},
    )
    removed: httpx.Response = post_form(
        admin,
        "/admin/access",
        "/admin/access/rules/delete",
        {"rule_index": viewers_indexes.group(1), "grant_index": viewers_indexes.group(2)},
    )

    assert locked_out.status_code == 400
    assert "nobody who can administer" in locked_out.text
    assert removed.status_code == 303
    assert "</span>viewers</span>" not in admin.get("/admin/access").text


# ----------------------------------------------------------------------------------------------------------------------
def test_10_the_login_settings_are_changed_unless_that_would_lock_everyone_out(admin: httpx.Client) -> None:
    longer: httpx.Response = post_form(
        admin,
        "/admin/access",
        "/admin/access/settings",
        {"session_duration_minutes": "60", "is_local_enabled": "on"},
    )
    no_local_logins: httpx.Response = post_form(
        admin, "/admin/access", "/admin/access/settings", {"session_duration_minutes": "60"}
    )

    assert longer.status_code == 303
    assert 'value="60"' in admin.get("/admin/access").text
    assert no_local_logins.status_code == 400
    assert "lock everyone out" in no_local_logins.text


# ----------------------------------------------------------------------------------------------------------------------
def test_11_an_oidc_provider_is_added_with_its_secret_changed_and_removed(
    admin: httpx.Client, repo_root: Path
) -> None:
    provider: dict[str, str | list[str]] = {
        "provider_name": "authentik",
        "label": "Company login",
        "issuer": "https://auth.example.com/application/o/bearpond/",
        "client_id": "bearpond",
        "secret": "s3cret-from-the-provider",
        "scopes_text": "openid profile email",
        "groups_claim": "groups",
        "username_claim": "preferred_username",
        "name_claim": "name",
        "email_claim": "email",
        "logout": "standard",
    }
    bad_issuer: httpx.Response = post_form(
        admin, "/admin/access/providers/new", "/admin/access/providers", {**provider, "issuer": "auth.example.com"}
    )
    added: httpx.Response = post_form(admin, "/admin/access/providers/new", "/admin/access/providers", provider)
    access: httpx.Response = admin.get("/admin/access")
    edit_page: httpx.Response = admin.get("/admin/access/providers/authentik")
    changed: httpx.Response = post_form(
        admin,
        "/admin/access/providers/authentik",
        "/admin/access/providers/authentik",
        {**provider, "secret": "", "label": "Staff login"},
    )
    login_page: httpx.Response = admin.get("/login")
    stored_secret: str = (repo_root / "auth-secrets" / "authentik").read_text().strip()
    removed: httpx.Response = post_form(admin, "/admin/access", "/admin/access/providers/authentik/delete", {})

    assert bad_issuer.status_code == 400
    assert "An issuer is an https or http address." in bad_issuer.text
    assert 'value="Company login"' in bad_issuer.text
    assert added.status_code == 303
    assert "Company login" in access.text
    assert "stored" in access.text
    assert "http://127.0.0.1:8000/auth/oidc/authentik/callback" in edit_page.text
    assert "s3cret-from-the-provider" not in edit_page.text
    assert changed.status_code == 303
    assert stored_secret == "s3cret-from-the-provider"
    assert "Continue with Staff login" in login_page.text
    assert removed.status_code == 303
    assert "authentik" not in admin.get("/admin/access").text


# ----------------------------------------------------------------------------------------------------------------------
def test_12_an_admin_form_without_the_secret_of_the_page_is_refused(admin: httpx.Client) -> None:
    forged: httpx.Response = admin.post("/admin/groups", data={"_csrf": "forged", "group_name": "sneaky"})

    assert forged.status_code == 403
    assert "sneaky" not in admin.get("/admin/groups").text
