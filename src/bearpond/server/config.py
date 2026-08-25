import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_DIR_ENV_VAR = "BEARPOND_CONFIG_DIR"


# ======================================================================================================================
@dataclass
class ServerConfig:
    # path to the server-wide SQLite metadata database; defaults to bearpond.sqlite in the config dir
    db_path: Path
    # path to the server-wide content-addressed object store; defaults to objects/ in the config dir
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

    return ServerConfig(db_path=db_path, object_store_root=object_store_root)
