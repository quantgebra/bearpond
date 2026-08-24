import re
from pathlib import Path

from . import config
from . import metadata_store
from . import object_store
from . import repository
from .sqlite_server_metadata_store import SqliteServerMetadataStore


# ======================================================================================================================
class Server:
    # owns the server-wide MetadataStore and ObjectStore and vends Repository instances scoped to a single repo.
    # this is the single place server boot code initializes shared resources, so swapping SQLite for Postgres or the
    # local object store for S3 only requires changes here.

    _REPO_NAME: re.Pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, server_config: config.ServerConfig) -> None:
        self.config: config.ServerConfig = server_config
        self.metadata_store: metadata_store.ServerMetadataStore = SqliteServerMetadataStore(server_config.db_path)
        self.object_store: object_store.ObjectStore = object_store.ObjectStore(server_config.object_store_root)
        self._repos: dict[str, repository.Repository] = {}

    # ------------------------------------------------------------------------------------------------------------------
    def _repo_view(self, name: str) -> metadata_store.MetadataStore:
        return metadata_store.MetadataStore(name, self.metadata_store)

    # ------------------------------------------------------------------------------------------------------------------
    def get_repository(self, name: str) -> repository.Repository:
        if name not in self._repos:
            if not self._REPO_NAME.match(name):
                raise repository.RepositoryNotFoundError(f"invalid repository name: {name}")
            # a repository exists iff it is registered in the metadata store — there is no on-disk repo directory
            if name not in self.metadata_store.list_repos():
                raise repository.RepositoryNotFoundError(f"unknown repository: {name}")
            self._repos[name] = repository.Repository(name, self._repo_view(name), self.object_store)
        return self._repos[name]

    # ------------------------------------------------------------------------------------------------------------------
    def create_repository(self, name: str) -> repository.Repository:
        if not self._REPO_NAME.match(name):
            raise repository.InvalidRepositoryNameError(f"invalid repository name: {name}")
        if name in self.metadata_store.list_repos():
            raise repository.RepositoryExistsError(f"repository already exists: {name}")
        self.metadata_store.register_repo(name)
        self._repos[name] = repository.Repository(name, self._repo_view(name), self.object_store)
        return self._repos[name]

    # ------------------------------------------------------------------------------------------------------------------
    def list_repositories(self) -> list[str]:
        return self.metadata_store.list_repos()

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self._repos.clear()
        self.metadata_store.close()


# ======================================================================================================================
# module-level singleton for the running server process. tests and CLI entry points call configure() directly; when the
# server is launched with only BEARPOND_CONFIG_DIR set, get_server() auto-configures from the environment on first use.
_server_instance: Server | None = None


# ----------------------------------------------------------------------------------------------------------------------
def configure(server_config: config.ServerConfig) -> Server:
    global _server_instance
    close()
    _server_instance = Server(server_config)
    return _server_instance


# ----------------------------------------------------------------------------------------------------------------------
def get_server() -> Server:
    if _server_instance is None:
        config_dir: Path | None = config.config_dir_from_env()
        if config_dir is not None:
            return configure(config.load_server_config(config_dir))
        raise RuntimeError("bearpond server is not configured — set BEARPOND_CONFIG_DIR before startup")
    return _server_instance


# ----------------------------------------------------------------------------------------------------------------------
def close() -> None:
    global _server_instance
    if _server_instance is not None:
        _server_instance.close()
        _server_instance = None
