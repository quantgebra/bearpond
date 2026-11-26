import re
from pathlib import Path

import sqlalchemy
from sqlalchemy import orm

from . import config
from . import object_store
from . import repo_record
from . import repository
from . import server_auth
from . import server_database


# ======================================================================================================================
class Server:
    # owns the engine of the server's database and the server-wide ObjectStore, and vends Repository instances scoped
    # to a single repo and to the session of one request. this is the single place server boot code initializes shared
    # resources, so swapping SQLite for Postgres or the local object store for S3 only requires changes here.
    
    _REPO_NAME: re.Pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    
    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, server_config: config.ServerConfig) -> None:
        # we don't really ever use the config, but save for now
        self.config: config.ServerConfig = server_config
        
        # the engine of the database that everything is kept in, whose tables are brought up to date at every start
        database_url: str = server_config.database_url or f"sqlite:///{server_config.db_path}"
        server_config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: sqlalchemy.Engine = server_database.make_engine(database_url)
        server_database.upgrade(self.engine)
        
        # create the ObjectStore
        self.object_store: object_store.ObjectStore = object_store.ObjectStore(server_config.object_store_root)
        
        # who is making a request, and what they may do
        self.auth: server_auth.ServerAuth = server_auth.ServerAuth(server_config, self.engine)
    
    # ------------------------------------------------------------------------------------------------------------------
    def get_repository(self, session: orm.Session, name: str) -> repository.Repository:
        # ensure the name matches our rules
        if not self._REPO_NAME.match(name):
            raise repository.RepositoryNotFoundError(f"invalid repository name: {name}")
        
        result: repository.Repository
        if repo_record.RepoRecord.get_by_name(session, name) is not None:
            result = repository.Repository(name, session, self.object_store)
        else:
            # the caller is trying to get a repo that does not exist
            raise repository.RepositoryNotFoundError(f"unknown repository: {name}")

        return result
    
    # ------------------------------------------------------------------------------------------------------------------
    def create_repository(self, session: orm.Session, name: str) -> repository.Repository:
        # ensure the name matches our rules
        if not self._REPO_NAME.match(name):
            raise repository.InvalidRepositoryNameError(f"invalid repository name: {name}")
        
        # error on name collisions
        if repo_record.RepoRecord.get_by_name(session, name) is not None:
            raise repository.RepositoryExistsError(f"repository already exists: {name}")
        
        # create the repo, register it, and hand back the repository over the session that it was made in
        repo_record.RepoRecord.register(session, name)
        return repository.Repository(name, session, self.object_store)
    
    # ------------------------------------------------------------------------------------------------------------------
    def list_repositories(self, session: orm.Session) -> list[str]:
        return repo_record.RepoRecord.get_all_names(session)
    
    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self.engine.dispose()


# ======================================================================================================================
# module-level singleton for the running server process. tests and CLI entry points call configure() directly; when the
# server is launched with only BEARPOND_CONFIG_DIR set, get_server() auto-configures from the environment on first use.
_server_instance: Server | None = None


# ----------------------------------------------------------------------------------------------------------------------
def configure(server_config: config.ServerConfig) -> Server:
    global _server_instance
    if _server_instance is not None:
        raise RuntimeError("bearpond server is already configured — call close() before reconfiguring")
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
        result = configure(config.load_server_config(config_dir, config.static_token_from_env()))
    
    assert result is not None
    return result


# ----------------------------------------------------------------------------------------------------------------------
def close() -> None:
    global _server_instance
    if _server_instance is not None:
        _server_instance.close()
        _server_instance = None
