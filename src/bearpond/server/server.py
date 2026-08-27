import re
from pathlib import Path
from typing import Optional

from . import config
from . import metadata_store
from . import object_store
from . import repo_metadata_store
from . import repository
from . import sqlite_metadata_store


# ======================================================================================================================
class Server:
    # owns the server-wide MetadataStore and ObjectStore and vends Repository instances scoped to a single repo.
    # this is the single place server boot code initializes shared resources, so swapping SQLite for Postgres or the
    # local object store for S3 only requires changes here.

    _REPO_NAME: re.Pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, server_config: config.ServerConfig) -> None:
        # we don't really ever use the config, but save it anyhow
        self.config: config.ServerConfig = server_config
        
        # create the MetadataStore
        self.metadata_store: metadata_store.MetadataStore = (
            sqlite_metadata_store.SqliteMetadataStore(server_config.db_path))
        
        # create the ObjectStore
        self.object_store: object_store.ObjectStore = object_store.ObjectStore(server_config.object_store_root)
        
        # a name_2_repo map
        self._name_2_repo: dict[str, repository.Repository] = {}

    # ------------------------------------------------------------------------------------------------------------------
    def _repo_metadata_store(self, name: str) -> repo_metadata_store.RepoMetadataStore:
        return repo_metadata_store.RepoMetadataStore(name, self.metadata_store)

    # ------------------------------------------------------------------------------------------------------------------
    def get_repository(self, name: str) -> repository.Repository:
        # ensure the name matches our rules
        if not self._REPO_NAME.match(name):
            raise repository.RepositoryNotFoundError(f"invalid repository name: {name}")

        # first try to get it from the cache
        result: Optional[repository.Repository] = self._name_2_repo.get(name)

        if result is None:
            # we don't have the repo in the cache, let's try to create it
            
            # if we don't have a record of it in the MetadataStore, then raise an Error
            if name not in self.metadata_store.list_repos():
                raise repository.RepositoryNotFoundError(f"unknown repository: {name}")
            
            # create the repo and add it to the cache
            self._name_2_repo[name] = repository.Repository(name, self._repo_metadata_store(name), self.object_store)
            
            # get the result from the cache
            result = self._name_2_repo[name]

        # by this point every path above has populated result — tell the type checker so
        assert result is not None
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def create_repository(self, name: str) -> repository.Repository:
        # ensure the name matches our rules
        if not self._REPO_NAME.match(name):
            raise repository.InvalidRepositoryNameError(f"invalid repository name: {name}")
        
        # error on name collisions
        if name in self.metadata_store.list_repos():
            raise repository.RepositoryExistsError(f"repository already exists: {name}")
        
        # create the repo, register it with the metadata_store
        self.metadata_store.register_repo(name)
        self._name_2_repo[name] = repository.Repository(name, self._repo_metadata_store(name), self.object_store)
        
        # return
        return self._name_2_repo[name]

    # ------------------------------------------------------------------------------------------------------------------
    def list_repositories(self) -> list[str]:
        return self.metadata_store.list_repos()

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self._name_2_repo.clear()
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
    assert _server_instance is not None
    return _server_instance


# ----------------------------------------------------------------------------------------------------------------------
def get_server() -> Server:
    result: Server | None = _server_instance
    if result is None:
        config_dir: Path | None = config.config_dir_from_env()
        if config_dir is None:
            raise RuntimeError("bearpond server is not configured — set BEARPOND_CONFIG_DIR before startup")
        result = configure(config.load_server_config(config_dir))
        
    assert result is not None
    return result


# ----------------------------------------------------------------------------------------------------------------------
def close() -> None:
    global _server_instance
    if _server_instance is not None:
        _server_instance.close()
        _server_instance = None
