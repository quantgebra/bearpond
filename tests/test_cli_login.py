import argparse
import io
import json
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import orm
from quantgebra.auth.apikey import api_key_authentication
from quantgebra.auth.core import actor
from quantgebra.auth.core import user_record

import conftest
from bearpond.client import cli
from bearpond.client import credential_store
from bearpond.client import server_client
from bearpond.server import config
from bearpond.server import repository
from bearpond.server import server as server_module

HIVE_PATH: str = "year=2024/month=01/part.parquet"
CONTENT: bytes = b"fake parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def credentials_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # the credentials file of the test, never the real one in the home directory, and no token in the environment
    path: Path = tmp_path / "home" / ".config" / "bearpond" / "credentials"
    monkeypatch.setattr(cli, "CREDENTIALS_PATH", path)
    monkeypatch.delenv(cli.TOKEN_ENV_VAR, raising=False)
    return path


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def enforced_url(repo_root: Path) -> Iterator[str]:
    # a server that makes people log in, with a repository that has a file in it
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
            auth=config.AuthSettings(mode="enforced", secrets_dir=repo_root / "auth-secrets"),
        )
    )
    with orm.Session(srv.engine, expire_on_commit=False) as session:
        alpha_repo: repository.Repository = srv.create_repository(session, "alpha")
        session.commit()
        conftest.commit_files(alpha_repo, {HIVE_PATH: CONTENT})
    with conftest.running_server() as url:
        yield url
    server_module.close()


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def api_key(enforced_url: str) -> str:
    # the key of a service account in the viewers group, as an administrator makes one on the API keys page
    with server_module.get_server().auth.open_services() as services:
        administrator: actor.Actor = services.auth.get_development_actor()
        account: user_record.UserRecord = services.api_keys.create_service_account(
            administrator,
            api_key_authentication.ServiceAccountRequest(username="laptop", display_name="", group_names=["viewers"]),
        )
        new_key: api_key_authentication.NewApiKey = services.api_keys.create_key(
            administrator,
            api_key_authentication.ApiKeyRequest(user_id=account.user_id, key_name="laptop", expires_in_days=None),
        )
    return new_key.token


# ----------------------------------------------------------------------------------------------------------------------
def test_01_logging_in_checks_the_key_and_saves_it_for_the_server(
    enforced_url: str, api_key: str, credentials_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.cmd_login(argparse.Namespace(server=enforced_url, token=api_key))

    saved: dict = json.loads(credentials_path.read_text())
    printed: str = capsys.readouterr().out
    assert saved == {"servers": {enforced_url: api_key}}
    assert "You can read 1 repository: alpha" in printed


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_other_commands_send_the_saved_key(
    enforced_url: str, api_key: str, credentials_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.cmd_login(argparse.Namespace(server=enforced_url + "/", token=api_key))
    workdir: Path = tmp_path / "alpha"

    cli.cmd_clone(argparse.Namespace(url=f"{enforced_url}/alpha", dir=workdir, token=None, query=None))
    monkeypatch.chdir(workdir)
    cli.cmd_log(argparse.Namespace(token=None, limit=5))

    assert (workdir / HIVE_PATH).read_bytes() == CONTENT


# ----------------------------------------------------------------------------------------------------------------------
def test_03_a_key_that_the_server_refuses_is_not_saved(enforced_url: str, credentials_path: Path) -> None:
    with pytest.raises(server_client.BearpondError, match="did not accept the key"):
        cli.cmd_login(argparse.Namespace(server=enforced_url, token="bp_not-a-key"))

    assert not credentials_path.exists()


# ----------------------------------------------------------------------------------------------------------------------
def test_04_the_token_option_and_the_environment_come_before_the_saved_key(
    enforced_url: str, api_key: str, credentials_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.cmd_login(argparse.Namespace(server=enforced_url, token=api_key))

    saved_token: str | None = cli.resolve_token(None, enforced_url)
    monkeypatch.setenv(cli.TOKEN_ENV_VAR, "bp_from-the-environment")
    environment_token: str | None = cli.resolve_token(None, enforced_url)
    option_token: str | None = cli.resolve_token("bp_from-the-option", enforced_url)

    assert saved_token == api_key
    assert environment_token == "bp_from-the-environment"
    assert option_token == "bp_from-the-option"


# ----------------------------------------------------------------------------------------------------------------------
def test_05_logging_out_forgets_the_key_and_keeps_the_others(
    enforced_url: str, api_key: str, credentials_path: Path, tmp_path: Path
) -> None:
    store: credential_store.CredentialStore = credential_store.CredentialStore(credentials_path)
    store.save_token("https://other.example.com", "bp_other")
    cli.cmd_login(argparse.Namespace(server=enforced_url, token=api_key))

    cli.cmd_logout(argparse.Namespace(server=enforced_url))

    assert store.get_token(enforced_url) is None
    assert store.get_token("https://other.example.com") == "bp_other"
    with pytest.raises(server_client.BearpondError, match="401"):
        cli.cmd_clone(argparse.Namespace(url=f"{enforced_url}/alpha", dir=tmp_path / "alpha", token=None, query=None))


# ----------------------------------------------------------------------------------------------------------------------
def test_06_a_key_can_be_piped_in_instead_of_typed(
    enforced_url: str, api_key: str, credentials_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(api_key + "\n"))

    cli.cmd_login(argparse.Namespace(server=enforced_url, token=None))

    assert credential_store.CredentialStore(credentials_path).get_token(enforced_url) == api_key


# ----------------------------------------------------------------------------------------------------------------------
def test_07_one_server_is_one_address_however_it_is_typed() -> None:
    assert credential_store.normalize_server_url("HTTPS://Data.Example.com/") == "https://data.example.com"
    assert credential_store.normalize_server_url(" http://127.0.0.1:8000 ") == "http://127.0.0.1:8000"
    assert credential_store.normalize_server_url("https://example.com/bearpond/") == "https://example.com/bearpond"


# ----------------------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not have these permission bits")
def test_08_only_its_owner_can_read_the_credentials_file(
    enforced_url: str, api_key: str, credentials_path: Path
) -> None:
    cli.cmd_login(argparse.Namespace(server=enforced_url, token=api_key))

    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials_path.parent.stat().st_mode) == 0o700
