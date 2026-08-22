import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_DIR_ENV_VAR = "BEARPOND_CONFIG_DIR"


# ======================================================================================================================
@dataclass
class ServerConfig:
    # the directory that holds the server's repositories — one subdirectory per repo
    repos_root: Path
    # path to the server-wide SQLite metadata database; defaults to a file named bearpond.sqlite inside repos_root
    db_path: Path
    # path to the server-wide content-addressed object store; defaults to <repos_root>/objects
    object_store_root: Path


# ----------------------------------------------------------------------------------------------------------------------
def config_dir_from_env() -> Path | None:
    value: str | None = os.environ.get(CONFIG_DIR_ENV_VAR)
    return Path(value).expanduser().resolve() if value else None


# ----------------------------------------------------------------------------------------------------------------------
def load_server_config(config_dir: Path) -> ServerConfig:
    config_path: Path = config_dir / "server.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"no server.yaml found in config dir: {config_dir}")

    raw: dict = yaml.safe_load(config_path.read_text()) or {}
    if "repos_root" not in raw:
        raise ValueError(f"server.yaml missing required key 'repos_root' ({config_path})")

    # a relative repos_root is resolved against the config file's own directory, not the process's cwd — so the
    # same config works regardless of where the server happens to be launched from
    repos_root: Path = Path(raw["repos_root"]).expanduser()
    if not repos_root.is_absolute():
        repos_root = (config_dir / repos_root).resolve()

    # db_path defaults to repos_root/bearpond.sqlite; relative paths resolve against the config dir
    db_path: Path = Path(raw.get("db_path", repos_root / "bearpond.sqlite")).expanduser()
    if not db_path.is_absolute():
        db_path = (config_dir / db_path).resolve()

    # object_store_root defaults to repos_root/objects; relative paths resolve against the config dir
    object_store_root: Path = Path(raw.get("object_store_root", repos_root / "objects")).expanduser()
    if not object_store_root.is_absolute():
        object_store_root = (config_dir / object_store_root).resolve()

    return ServerConfig(repos_root=repos_root, db_path=db_path, object_store_root=object_store_root)
