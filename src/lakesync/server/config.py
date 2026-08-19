import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_DIR_ENV_VAR = "LAKESYNC_CONFIG_DIR"


# ======================================================================================================================
@dataclass
class ServerConfig:
    repo_root: Path


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
    if "repo_root" not in raw:
        raise ValueError(f"server.yaml missing required key 'repo_root' ({config_path})")

    # a relative repo_root is resolved against the config file's own directory, not the process's cwd — so the
    # same config works regardless of where the server happens to be launched from
    repo_root: Path = Path(raw["repo_root"]).expanduser()
    if not repo_root.is_absolute():
        repo_root = (config_dir / repo_root).resolve()

    return ServerConfig(repo_root=repo_root)
