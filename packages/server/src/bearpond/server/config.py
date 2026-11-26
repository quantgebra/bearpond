import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR_ENV_VAR = "BEARPOND_CONFIG_DIR"
STATIC_TOKEN_ENV_VAR = "BEARPOND_TOKEN"
AUTH_MODES: list[str] = ["none", "enforced"]


# ======================================================================================================================
@dataclass
class AuthSettings:
    # "none" lets everyone in (for local use), "enforced" makes people log in
    mode: str = "none"
    # the address that people reach the server at, which invitation links and login redirects are made from
    base_url: str = "http://127.0.0.1:8000"
    # how long a login lasts
    session_duration_minutes: int = 480
    # the folder that the client secrets of OIDC providers are kept in; None puts it next to the metadata database
    secrets_dir: Path | None = None


# ======================================================================================================================
@dataclass
class ServerConfig:
    # path to the server-wide SQLite metadata database; defaults to bearpond.sqlite in the config dir
    db_path: Path
    # path to the server-wide content-addressed object store; defaults to objects/ in the config dir
    object_store_root: Path
    # where everything that the server keeps in a database is: a SQLAlchemy url, such as sqlite:///bearpond.sqlite or
    # postgresql+psycopg://user:password@host/database; None means the SQLite file at db_path
    database_url: str | None = None
    # how people log in and what they may do
    auth: AuthSettings = field(default_factory=AuthSettings)
    # the one shared secret that scripts and the command line send as a bearer token until they have tokens of their
    # own; None means there is none
    static_token: str | None = None


# ----------------------------------------------------------------------------------------------------------------------
def config_dir_from_env() -> Path | None:
    value: str | None = os.environ.get(CONFIG_DIR_ENV_VAR)
    return Path(value).expanduser().resolve() if value else None


# ----------------------------------------------------------------------------------------------------------------------
def static_token_from_env() -> str | None:
    value: str | None = os.environ.get(STATIC_TOKEN_ENV_VAR)
    return value or None


# ----------------------------------------------------------------------------------------------------------------------
def _resolve_path(raw_path: str, config_dir: Path) -> Path:
    # relative paths resolve against the config dir, not the process's cwd
    resolved: Path = Path(raw_path).expanduser()
    if not resolved.is_absolute():
        resolved = (config_dir / resolved).resolve()
    return resolved


# ----------------------------------------------------------------------------------------------------------------------
def _load_auth_settings(raw_auth: dict, config_dir: Path) -> AuthSettings:
    mode: str = str(raw_auth.get("mode", "none"))
    if mode not in AUTH_MODES:
        raise ValueError(f"auth.mode must be one of {AUTH_MODES}, not {mode}")

    return AuthSettings(
        mode=mode,
        base_url=str(raw_auth.get("base_url", "http://127.0.0.1:8000")),
        session_duration_minutes=int(raw_auth.get("session_duration_minutes", 480)),
        secrets_dir=_resolve_path(str(raw_auth.get("secrets_dir", "auth-secrets")), config_dir),
    )


# ----------------------------------------------------------------------------------------------------------------------
def load_server_config(config_dir: Path, static_token: str | None = None) -> ServerConfig:
    config_path: Path = config_dir / "server.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"no server.yaml found in config dir: {config_dir}")

    raw: dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # db_path defaults to bearpond.sqlite next to the config file; relative paths resolve against the config dir,
    # not the process's cwd — so the same config works regardless of where the server happens to be launched from
    db_path: Path = Path(raw.get("db_path", "bearpond.sqlite")).expanduser()
    if not db_path.is_absolute():
        db_path = (config_dir / db_path).resolve()

    # object_store_root defaults to objects/ next to the config file; relative paths resolve against the config dir
    object_store_root: Path = Path(raw.get("object_store_root", "objects")).expanduser()
    if not object_store_root.is_absolute():
        object_store_root = (config_dir / object_store_root).resolve()

    # the auth section is optional: without it everyone is let in, as before there was any
    auth_settings: AuthSettings = _load_auth_settings(raw.get("auth") or {}, config_dir)

    return ServerConfig(
        db_path=db_path,
        object_store_root=object_store_root,
        database_url=raw.get("database_url"),
        auth=auth_settings,
        static_token=static_token,
    )
