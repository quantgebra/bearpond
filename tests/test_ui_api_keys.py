import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import orm
from quantgebra.auth.local import local_authentication

import conftest
from bearpond.client import server_client
from bearpond.protocol import types
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
def accept_invite(browser: httpx.Client, link_url: str) -> None:
    invite_path: str = "/invite/" + link_url.rsplit("/", 1)[1]
    response: httpx.Response = browser.post(
        invite_path, data={"password": STRONG_PASSWORD, "password_again": STRONG_PASSWORD}
    )
    assert response.status_code == 303


# ----------------------------------------------------------------------------------------------------------------------
def post_form(
    browser: httpx.Client, page_path: str, action_path: str, data: dict[str, str | list[str]]
) -> httpx.Response:
    # submits a form of a page the way a browser does: with the secret that the page carries
    page: httpx.Response = browser.get(page_path)
    match: re.Match[str] | None = re.search(r'name="_csrf" value="([^"]*)"', page.text)
    assert match is not None
    form_data: dict[str, str | list[str]] = {"_csrf": match.group(1)}
    form_data.update(data)
    return browser.post(action_path, data=form_data)


# ----------------------------------------------------------------------------------------------------------------------
def find_shown_secret(html: str) -> str:
    # the secret of a key that was just made, which the page shows once
    match: re.Match[str] | None = re.search(r'class="copy-field" value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


# ----------------------------------------------------------------------------------------------------------------------
def make_key(browser: httpx.Client, key_name: str, data: dict[str, str | list[str]] | None = None) -> str:
    # makes a key on the page of keys and returns its secret
    response: httpx.Response = post_form(
        browser, "/account/keys", "/account/keys", {"key_name": key_name, **(data or {})}
    )
    assert response.status_code == 200
    return find_shown_secret(response.text)


# ----------------------------------------------------------------------------------------------------------------------
def make_viewer(admin: httpx.Client, enforced_url: str) -> httpx.Client:
    # a person in the viewers group, logged in in a browser of their own, which the test closes
    invited: httpx.Response = post_form(
        admin, "/admin/users", "/admin/users/invite", {"username": "viv", "groups": ["viewers"]}
    )
    viewer: httpx.Client = httpx.Client(base_url=enforced_url, timeout=30.0)
    accept_invite(viewer, find_shown_secret(invited.text))
    return viewer


# ----------------------------------------------------------------------------------------------------------------------
def test_01_a_person_makes_a_key_whose_secret_is_shown_once(admin: httpx.Client) -> None:
    made: httpx.Response = post_form(admin, "/account/keys", "/account/keys", {"key_name": "laptop"})
    secret: str = find_shown_secret(made.text)
    listing: httpx.Response = admin.get("/account/keys")

    assert secret.startswith("bp_")
    assert "laptop" in listing.text
    assert secret[:8] in listing.text
    assert secret not in listing.text


# ----------------------------------------------------------------------------------------------------------------------
def test_02_a_script_sends_the_key_and_may_do_what_its_user_may(admin: httpx.Client, enforced_url: str) -> None:
    viewer: httpx.Client = make_viewer(admin, enforced_url)
    secret: str = make_key(viewer, "notebook")
    viewer.close()

    with httpx.Client(base_url=enforced_url, headers={"Authorization": f"Bearer {secret}"}) as script:
        listing: httpx.Response = script.get("/repos")
        manifest: httpx.Response = script.get("/repos/alpha/manifest")
        create: httpx.Response = script.post("/repos", json={"name": "gamma"})
    with httpx.Client(base_url=enforced_url, headers={"Authorization": "Bearer bp_not-a-key"}) as stranger:
        refused: httpx.Response = stranger.get("/repos")

    assert [info["name"] for info in listing.json()] == ["alpha", "beta"]
    assert manifest.status_code == 200
    assert create.status_code == 403
    assert refused.status_code == 401


# ----------------------------------------------------------------------------------------------------------------------
def test_03_a_revoked_key_stops_working(admin: httpx.Client, enforced_url: str) -> None:
    secret: str = make_key(admin, "temporary")
    keys_page: httpx.Response = admin.get("/account/keys")
    match: re.Match[str] | None = re.search(r'action="/account/keys/([0-9a-f]+)/revoke"', keys_page.text)
    assert match is not None

    with httpx.Client(base_url=enforced_url, headers={"Authorization": f"Bearer {secret}"}) as script:
        before: httpx.Response = script.get("/repos")
        revoked: httpx.Response = post_form(admin, "/account/keys", f"/account/keys/{match.group(1)}/revoke", {})
        after: httpx.Response = script.get("/repos")

    assert before.status_code == 200
    assert revoked.status_code == 303
    assert after.status_code == 401


# ----------------------------------------------------------------------------------------------------------------------
def test_04_ci_commits_with_the_key_of_a_service_account(
    admin: httpx.Client, enforced_url: str, tmp_path: Path
) -> None:
    made: httpx.Response = post_form(
        admin,
        "/account/keys",
        "/account/keys/service-accounts",
        {"username": "ci", "display_name": "Nightly ETL", "groups": ["writers"]},
    )
    user_id_match: re.Match[str] | None = re.search(r'<option value="([0-9a-f]+)" selected>Nightly ETL', made.text)
    assert user_id_match is not None
    secret: str = make_key(admin, "ci-main", {"user_id": user_id_match.group(1)})

    # the client of the command line, given the key the way BEARPOND_TOKEN gives it
    local_file: Path = tmp_path / "bars.parquet"
    local_file.write_bytes(b"daily bars")
    with server_client.ServerClient(enforced_url, "alpha", token=secret) as client:
        files: dict[str, Path] = {"date=2026-11-26/bars.parquet": local_file}
        declared: list[types.FileMetadata] = server_client.ServerClient.declare_files(files)
        txn_uuid: types.TxnUuid = client.begin_transaction(types.TransactionManifest(added=declared))
        client.upload_files(txn_uuid, files)
        committed: types.CommitResponse = client.commit(txn_uuid)
    users: httpx.Response = admin.get("/admin/users")

    assert made.status_code == 200
    assert "The service account ci was made." in made.text
    assert committed.seq == 2
    assert "service account" in users.text
    assert "API keys only" in users.text


# ----------------------------------------------------------------------------------------------------------------------
def test_05_only_administrators_make_keys_for_others_and_service_accounts(
    admin: httpx.Client, enforced_url: str
) -> None:
    admin_page: httpx.Response = admin.get("/account/keys")
    admin_id_match: re.Match[str] | None = re.search(r'<option value="([0-9a-f]+)" selected>', admin_page.text)
    assert admin_id_match is not None
    viewer: httpx.Client = make_viewer(admin, enforced_url)
    viewer_page: httpx.Response = viewer.get("/account/keys")
    for_someone_else: httpx.Response = post_form(
        viewer, "/account/keys", "/account/keys", {"key_name": "sneaky", "user_id": admin_id_match.group(1)}
    )
    service_account: httpx.Response = post_form(
        viewer, "/account/keys", "/account/keys/service-accounts", {"username": "ci"}
    )
    viewer.close()

    assert "Every key" in admin_page.text
    assert "Every key" not in viewer_page.text
    assert "Make a service account" not in viewer_page.text
    assert for_someone_else.status_code == 403
    assert service_account.status_code == 403


# ----------------------------------------------------------------------------------------------------------------------
def test_06_without_logins_there_are_no_keys(live_server: str) -> None:
    with httpx.Client(base_url=live_server, timeout=30.0) as browser:
        page: httpx.Response = browser.get("/account/keys")

    assert page.status_code == 200
    assert "There are no API keys while the server lets everyone in" in page.text
