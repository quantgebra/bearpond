import asyncio
import hashlib
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from lakesync import types
from lakesync.server import config
from lakesync.server import repository
from lakesync.server import transaction
from lakesync.server.api import app as app_module


# ----------------------------------------------------------------------------------------------------------------------
def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ----------------------------------------------------------------------------------------------------------------------
def file_meta(rel_path: str, content: bytes) -> types.FileMeta:
    # builds the wire shape a client would send for a file with the given content
    return types.FileMeta(path=rel_path, size=len(content), sha256=sha256_hex(content))


# ----------------------------------------------------------------------------------------------------------------------
def write_lake_file(repo_root: Path, rel_path: str, content: bytes) -> Path:
    # drops a file straight into data/, bypassing the transaction machinery, for tests that need a pre-existing lake
    path: Path = repo_root / "data" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ----------------------------------------------------------------------------------------------------------------------
async def one_chunk_stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ----------------------------------------------------------------------------------------------------------------------
def upload_bytes(txn: transaction.Transaction, rel_path: str, data: bytes) -> types.FileMeta:
    # Transaction.upload_file is async (it streams the request body); tests drive it synchronously
    return asyncio.run(txn.upload_file(rel_path, one_chunk_stream(data)))


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


# ======================================================================================================================
@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root: Path = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    return root


# ======================================================================================================================
@pytest.fixture
def repo(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> repository.Repository:
    # points the process-wide singleton at this test's fresh repo_root — the app resolves it per request, so
    # reconfiguring here is enough for both domain-level and HTTP-level tests
    monkeypatch.delenv("LAKESYNC_TOKEN", raising=False)
    repository.configure(config.ServerConfig(repo_root=repo_root))
    return repository.get_repository()


# ======================================================================================================================
@pytest.fixture
def live_server(repo: repository.Repository) -> Iterator[str]:
    with running_server() as url:
        yield url
