import asyncio
import hashlib
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from bearpond import types
from bearpond.server import config
from bearpond.server import metadata_store
from bearpond.server import repository
from bearpond.server import server as server_module
from bearpond.server import transaction
from bearpond.server.api import app as app_module


# ----------------------------------------------------------------------------------------------------------------------
def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ----------------------------------------------------------------------------------------------------------------------
def file_meta(rel_path: str, content: bytes) -> types.FileMetadata:
    # builds the wire shape a client would send for a file with the given content
    return types.FileMetadata(path=rel_path, sha256=sha256_hex(content), size=len(content))


# ----------------------------------------------------------------------------------------------------------------------
def commit_files(repo: repository.Repository, files: dict[str, bytes]) -> types.CommitResponse:
    # drives files through the full transaction machinery — the standard way tests set up lake state
    added: list[types.FileMetadata] = [file_meta(path, content) for path, content in files.items()]
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(added=added))
    for path, content in files.items():
        upload_bytes(repo, txn.txn_uuid, path, content)
    return repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def remove_files(repo: repository.Repository, files: dict[str, bytes]) -> types.CommitResponse:
    # a removal-only transaction over files known to be in the lake with the given content
    removed: list[types.FileMetadata] = [file_meta(path, content) for path, content in files.items()]
    txn: transaction.Transaction = repo.begin_transaction(types.TransactionManifest(removed=removed))
    return repo.commit(txn.txn_uuid)


# ----------------------------------------------------------------------------------------------------------------------
def object_path(repo: repository.Repository, content: bytes) -> Path:
    # where the object store keeps the given content — the name is the hash
    return repo.object_store.get_location_for_address(sha256_hex(content))


# ----------------------------------------------------------------------------------------------------------------------
def current_manifest(repo: repository.Repository) -> types.Manifest | None:
    # the manifest at the lake's current seq — None on a never-committed lake
    store: metadata_store.MetadataStore = repo.metadata_store
    return store.get_manifest(store.get_current_seq())


# ----------------------------------------------------------------------------------------------------------------------
async def one_chunk_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ----------------------------------------------------------------------------------------------------------------------
def upload_bytes(repo: repository.Repository, txn_uuid: str, rel_path: str, data: bytes) -> types.FileMetadata:
    # Repository.upload_file is async (it streams the request body); tests drive it synchronously
    return asyncio.run(repo.upload_file(txn_uuid, rel_path, one_chunk_stream(data)))


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
@pytest.fixture
def repo(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[repository.Repository]:
    # points the process-wide registry at this test's fresh repos root, with one repository created inside it —
    # the app resolves repos per request, so reconfiguring here is enough for domain- and HTTP-level tests alike
    monkeypatch.delenv("BEARPOND_TOKEN", raising=False)
    srv: server_module.Server = server_module.configure(
        config.ServerConfig(
            db_path=repo_root / "bearpond.sqlite",
            object_store_root=repo_root / "objects",
        )
    )
    instance: repository.Repository = srv.create_repository(REPO_NAME)
    yield instance
    server_module.close()


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def live_server(repo: repository.Repository) -> Iterator[str]:
    with running_server() as url:
        yield url
