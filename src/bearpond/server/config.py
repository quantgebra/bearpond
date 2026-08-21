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

    return ServerConfig(repos_root=repos_root)
