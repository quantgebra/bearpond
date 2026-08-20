import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import httpx

from .. import types

CHUNK_SIZE = 1024 * 1024


# ======================================================================================================================
class BearpondError(Exception):
    # the one client-side error type, shared by the transport layer here and the workspace layer in
    # bearpond_client.py (it lives here because bearpond_client imports this module, not the other way around)
    pass


# ----------------------------------------------------------------------------------------------------------------------
def hash_and_size(local_path: Path) -> tuple[str, int]:
    # streams the file in chunks rather than reading it whole — files here can be multiple GB
    digest: hashlib._Hash = hashlib.sha256()
    size: int = 0
    with local_path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ----------------------------------------------------------------------------------------------------------------------
def iter_file_chunks(local_path: Path) -> Iterator[bytes]:
    # the request body for an upload — reads and yields chunks so the client never holds more than one chunk of a
    # large file in memory at a time
    with local_path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            yield chunk


# ======================================================================================================================
class ServerClient:

    # the transport layer: a thin wrapper over the bearpond HTTP protocol. Knows nothing about local disk
    # state — workspace concerns (synced state, staging, status) live in bearpond_client.BearpondClient.

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        response.read()  # safe no-op if already read; needed for streamed responses
        if not response.is_success:
            detail: str
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise BearpondError(f"{response.status_code}: {detail}")

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def declare_files(files: dict[str, Path]) -> list[types.FileMetadata]:
        # pure local hashing, no network call — builds the added-file list begin_transaction() needs, from a mapping
        # of hive-relative path to local file path
        declared: list[types.FileMetadata] = []
        for rel_path, local_path in files.items():
            sha256, size = hash_and_size(local_path)
            declared.append(types.FileMetadata(path=rel_path, size=size, sha256=sha256))
        return declared

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, base_url: str, token: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        # transport is injectable so tests (or exotic deployments, e.g. unix sockets) can swap how requests are carried
        self.token: str | None = token
        self._client: httpx.Client = httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0, transport=transport)

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------------------------------------------------------
    def __enter__(self) -> "ServerClient":
        return self

    # ------------------------------------------------------------------------------------------------------------------
    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # --- reads -----------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def get_manifest(self) -> types.Manifest:
        response: httpx.Response = self._client.get("/manifest", headers=self._headers())

        manifest: types.Manifest
        if response.status_code == 404:
            # nothing has ever been committed to this lake yet — a legitimate empty state, not an error
            manifest = types.Manifest(seq=0, created_at=None, files=[])
        else:
            self._raise_for_status(response)
            manifest = types.Manifest.model_validate_json(response.text)
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def download_file(self, rel_path: str, dest_path: Path) -> str:
        # streams to a temp file while hashing chunk by chunk, then renames into place — never buffers the whole
        # file in memory and never leaves a partial file at dest_path if the transfer is interrupted
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        digest: hashlib._Hash = hashlib.sha256()
        with self._client.stream("GET", f"/files/{rel_path}", headers=self._headers()) as response:
            self._raise_for_status(response)
            with tmp_path.open("wb") as f:
                for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                    digest.update(chunk)
                    f.write(chunk)
        checksum: str = digest.hexdigest()
        os.replace(tmp_path, dest_path)
        return checksum

    # --- transaction lifecycle --------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def begin_transaction(self, added: list[types.FileMetadata], removed: list[types.FileMetadata] | None = None) -> str:
        body: types.TransactionManifest = types.TransactionManifest(added=added, removed=removed or [])
        response: httpx.Response = self._client.post("/transactions", json=body.model_dump(), headers=self._headers())
        self._raise_for_status(response)
        result: types.BeginTransactionResponse = types.BeginTransactionResponse.model_validate_json(
            response.text
        )
        return result.txn_uuid

    # ------------------------------------------------------------------------------------------------------------------
    def upload_file(self, txn_uuid: str, rel_path: str, local_path: Path) -> types.FileMetadata:
        response: httpx.Response = self._client.put(
            f"/transactions/{txn_uuid}/files/{rel_path}", content=iter_file_chunks(local_path), headers=self._headers()
        )
        self._raise_for_status(response)
        result: types.FileMetadata = types.FileMetadata.model_validate_json(response.text)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def upload_files(self, txn_uuid: str, files: dict[str, Path]) -> list[types.FileMetadata]:
        # uploads into an already-open transaction; doesn't begin, commit, or abort it — that's the caller's own
        # call to make: begin_transaction() -> upload_files() -> commit() or abort()
        results: list[types.FileMetadata] = []
        for rel_path, local_path in files.items():
            results.append(self.upload_file(txn_uuid, rel_path, local_path))
        return results

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, txn_uuid: str, reason: str | None = None) -> types.CommitResponse:
        body: types.CommitRequest = types.CommitRequest(reason=reason)
        response: httpx.Response = self._client.post(
            f"/transactions/{txn_uuid}/commit", json=body.model_dump(), headers=self._headers()
        )
        self._raise_for_status(response)
        result: types.CommitResponse = types.CommitResponse.model_validate_json(response.text)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def abort(self, txn_uuid: str) -> None:
        response: httpx.Response = self._client.delete(f"/transactions/{txn_uuid}", headers=self._headers())
        self._raise_for_status(response)

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction_status(self, txn_uuid: str) -> types.TransactionStatus:
        # tells a caller whether a transaction is still open or already committed — the way to learn the outcome
        # of a commit whose response was lost (retrying commit() itself is also safe: it's idempotent)
        response: httpx.Response = self._client.get(f"/transactions/{txn_uuid}", headers=self._headers())
        self._raise_for_status(response)
        result: types.TransactionStatus = types.TransactionStatus.model_validate_json(response.text)
        return result
