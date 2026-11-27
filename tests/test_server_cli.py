import argparse
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import conftest
from bearpond.server import cli
from bearpond.server import config
from bearpond.server import server as server_module

SERVER_YAML: str = """auth:
  mode: enforced
  base_url: http://127.0.0.1:8800
"""


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # a folder with a server.yaml in it, as a deploy writes one, and nothing about the server in the environment
    folder: Path = tmp_path / "config"
    folder.mkdir()
    (folder / "server.yaml").write_text(SERVER_YAML, encoding="utf-8")
    for env_var in [config.CONFIG_DIR_ENV_VAR, config.STATIC_TOKEN_ENV_VAR, cli.DATABASE_URL_ENV_VAR]:
        monkeypatch.delenv(env_var, raising=False)
    return folder


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def enforced_url(config_dir: Path) -> Iterator[str]:
    server_module.configure(cli.resolve_server_config(config_dir))
    with conftest.running_server() as url:
        yield url
    server_module.close()


# ----------------------------------------------------------------------------------------------------------------------
def test_01_the_health_check_answers_before_anyone_has_logged_in(enforced_url: str) -> None:
    with httpx.Client(base_url=enforced_url, timeout=30.0) as client:
        health: httpx.Response = client.get("/health")
        repositories: httpx.Response = client.get("/repos")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert repositories.status_code == 401


# ----------------------------------------------------------------------------------------------------------------------
def test_02_the_settings_are_those_of_the_folder_given_or_named_by_the_environment(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_dir: Path = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "server.yaml").write_text("db_path: elsewhere.sqlite\n", encoding="utf-8")
    monkeypatch.setenv(config.CONFIG_DIR_ENV_VAR, str(other_dir))

    from_environment: config.ServerConfig = cli.resolve_server_config(None)
    from_option: config.ServerConfig = cli.resolve_server_config(config_dir)

    assert from_environment.db_path == other_dir / "elsewhere.sqlite"
    assert from_option.db_path == config_dir / "bearpond.sqlite"
    assert from_option.auth.mode == "enforced"


# ----------------------------------------------------------------------------------------------------------------------
def test_03_the_database_address_and_the_shared_token_come_from_the_environment(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.DATABASE_URL_ENV_VAR, "postgresql+psycopg://bearpond:secret@db.internal/bearpond")
    monkeypatch.setenv(config.STATIC_TOKEN_ENV_VAR, "shared-token")

    server_config: config.ServerConfig = cli.resolve_server_config(config_dir)

    assert server_config.database_url == "postgresql+psycopg://bearpond:secret@db.internal/bearpond"
    assert server_config.static_token == "shared-token"


# ----------------------------------------------------------------------------------------------------------------------
def test_04_without_a_folder_there_is_nothing_to_run(config_dir: Path) -> None:
    with pytest.raises(SystemExit, match="--config-dir"):
        cli.resolve_server_config(None)


# ----------------------------------------------------------------------------------------------------------------------
def test_05_the_person_who_runs_the_server_makes_links_with_the_auth_command(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first_exit_code: int = cli.cmd_auth(argparse.Namespace(config_dir=config_dir, auth_args=["first-administrator"]))
    first_printed: str = capsys.readouterr().out
    invite_exit_code: int = cli.cmd_auth(
        argparse.Namespace(config_dir=config_dir, auth_args=["create-invite", "carol", "--group", "viewers"])
    )
    invite_printed: str = capsys.readouterr().out
    unknown_exit_code: int = cli.cmd_auth(argparse.Namespace(config_dir=config_dir, auth_args=["reset-link", "nobody"]))

    assert first_exit_code == 0
    assert "http://127.0.0.1:8800/invite/" in first_printed
    assert invite_exit_code == 0
    assert 'The account "carol" is made.' in invite_printed
    assert unknown_exit_code == 1
