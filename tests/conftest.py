import asyncio
import hashlib
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy
import uvicorn
from testcontainers.community import postgres
from sqlalchemy import orm

from bearpond.protocol import types
from bearpond.server import config
from bearpond.server import repository
from bearpond.server import server as server_module
from bearpond.server import transaction
from bearpond.server.api import app as app_module


# ----------------------------------------------------------------------------------------------------------------------
def sha256_hex(content: bytes) -> types.Sha256:
    return types.Sha256(hashlib.sha256(content).hexdigest())


# ----------------------------------------------------------------------------------------------------------------------
def file_meta(rel_path: str, content: bytes) -> types.FileMetadata:
    # builds the wire shape a client would send for a file with the given content
    return types.FileMetadata(path=rel_path, sha256=sha256_hex(content), size=len(content))


# ----------------------------------------------------------------------------------------------------------------------
def commit_files(repo: repository.Repository, files: dict[str, bytes]) -> types.CommitResponse:
    # drives files through the full transaction machinery — the standard way tests set up lake state
    added: list[types.FileMetadata] = []
    for path, content in files.items():
        added.append(file_meta(path, content))
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(added=added))
    for path, content in files.items():
        upload_bytes(repo, txn, path, content)
    result: types.CommitResponse = repo.commit(txn)
    repo.session.commit()
    return result


# ----------------------------------------------------------------------------------------------------------------------
def remove_files(repo: repository.Repository, files: dict[str, bytes]) -> types.CommitResponse:
    # a removal-only transaction over files known to be in the lake with the given content
    removed: list[types.FileMetadata] = []
    for path, content in files.items():
        removed.append(file_meta(path, content))
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(removed=removed))
    result: types.CommitResponse = repo.commit(txn)
    repo.session.commit()
    return result


# ----------------------------------------------------------------------------------------------------------------------
def object_path(repo: repository.Repository, content: bytes) -> Path:
    # where the object store keeps the given content — the name is the hash
    return repo.object_store.get_location_for_address(sha256_hex(content))


# ----------------------------------------------------------------------------------------------------------------------
def current_manifest(repo: repository.Repository) -> types.Manifest | None:
    # the manifest at the lake's current seq — None on a never-committed lake
    return repo.get_manifest(repo.get_current_seq())


# ----------------------------------------------------------------------------------------------------------------------
async def one_chunk_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ----------------------------------------------------------------------------------------------------------------------
def upload_bytes(
    repo: repository.Repository, txn: transaction.Transaction, rel_path: str, data: bytes
) -> types.FileMetadata:
    # Repository.upload_file is async (it streams the request body); tests drive it synchronously
    return asyncio.run(repo.upload_file(txn, rel_path, one_chunk_stream(data)))


# ----------------------------------------------------------------------------------------------------------------------
@contextmanager
def running_server() -> Iterator[str]:
    # a real uvicorn on a loopback port, lifespan included — the sync httpx client can't ride an ASGI transport,
    # so HTTP-level tests exercise the actual stack instead of a test double
    sock: socket.socket = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()

    server: uvicorn.Server = uvicorn.Server(
        uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread: threading.Thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        deadline: float = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


REPO_NAME = "testrepo"


# ----------------------------------------------------------------------------------------------------------------------
def write_workspace_config(workdir: Path, server: str, repo: str = REPO_NAME) -> None:
    # a .bearpond/config.json as clone would have written it
    config_dir: Path = workdir / ".bearpond"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"server": server, "repo": repo}))


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    # the server's repos root — one subdirectory per repository
    root: Path = tmp_path / "repos"
    root.mkdir(parents=True)
    return root


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    # one throwaway PostgreSQL for the whole run; skipped where there is no docker to start it with
    try:
        container: postgres.PostgresContainer = postgres.PostgresContainer("postgres:17-alpine", driver="psycopg")
        container.start()
    except Exception as error:
        pytest.skip(f"no PostgreSQL to test with: {error}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


# ----------------------------------------------------------------------------------------------------------------------
def empty_postgres_tables(database_url: str) -> None:
    # every test starts with nothing in the tables, whoever made them
    engine: sqlalchemy.Engine = sqlalchemy.create_engine(database_url)
    with engine.begin() as connection:
        table_names: list[str] = []
        for schema_name in ("bearpond", "auth"):
            for table_name in sqlalchemy.inspect(connection).get_table_names(schema=schema_name):
                if not table_name.endswith("_alembic_version"):
                    table_names.append(f"{schema_name}.{table_name}")
        if table_names:
            connection.execute(sqlalchemy.text(f"TRUNCATE {', '.join(table_names)} RESTART IDENTITY CASCADE"))
    engine.dispose()


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def repo(
    request: pytest.FixtureRequest, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[repository.Repository]:
    # points the process-wide registry at this test's fresh repos root, with one repository created inside it —
    # the app resolves repos per request, so reconfiguring here is enough for domain- and HTTP-level tests alike; the
    # tests run on SQLite, which every machine has, and on PostgreSQL, which the server runs on
    monkeypatch.delenv("BEARPOND_TOKEN", raising=False)
    database_url: str | None = None
    if request.param == "postgres":
        database_url = request.getfixturevalue("postgres_url")
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
            database_url=database_url,
        )
    )
    session: orm.Session = orm.Session(srv.engine, expire_on_commit=False)
    instance: repository.Repository = srv.create_repository(session, REPO_NAME)
    session.commit()
    yield instance
    session.close()
    server_module.close()
    if database_url is not None:
        empty_postgres_tables(database_url)


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def reconfigure(
    repo_root: Path, repo: repository.Repository
) -> Callable[[str | None, config.AuthSettings | None], None]:
    # replaces the configured server with one that has the shared token and the authentication settings given, over the
    # same storage, so that the repository the repo fixture made is still there
    def replace_server(static_token: str | None, auth: config.AuthSettings | None) -> None:
        server_module.close()
        server_module.configure(
            config.ServerConfig(
                db_path=repo_root / "bearpond.sqlite",
                object_store_root=repo_root / "objects",
                database_url=repo.session.get_bind().url.render_as_string(hide_password=False),
                auth=auth or config.AuthSettings(),
                static_token=static_token,
            )
        )

    return replace_server


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def live_server(repo: repository.Repository) -> Iterator[str]:
    with running_server() as url:
        yield url
