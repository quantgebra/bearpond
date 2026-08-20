import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import httpx

from .. import types

CHUNK_SIZE = 1024 * 1024
SYNC_STATE_NAME = "_synced.json"


# ======================================================================================================================
class BearpondError(Exception):
    pass


# ======================================================================================================================
class SyncedFileState(NamedTuple):
    size: int
    sha256: str


# ----------------------------------------------------------------------------------------------------------------------
def load_local_state(target_dir: Path) -> dict[str, SyncedFileState]:
    # tracks what sync() has already fetched into target_dir, so a repeat sync only touches what actually changed
    state_path: Path = target_dir / SYNC_STATE_NAME
    state: dict[str, SyncedFileState] = {}
    if state_path.exists():
        raw: dict[str, list] = json.loads(state_path.read_text())
        state = {path: SyncedFileState(size=entry[0], sha256=entry[1]) for path, entry in raw.items()}
    return state


# ----------------------------------------------------------------------------------------------------------------------
def save_local_state(target_dir: Path, state: dict[str, SyncedFileState]) -> None:
    # write to a temp file then rename over the real one, so a reader never observes a half-written state file
    state_path: Path = target_dir / SYNC_STATE_NAME
    tmp_path: Path = state_path.with_suffix(".tmp")
    raw: dict[str, list] = {path: [entry.size, entry.sha256] for path, entry in state.items()}
    tmp_path.write_text(json.dumps(raw, indent=2))
    os.replace(tmp_path, state_path)


# ----------------------------------------------------------------------------------------------------------------------
def prune_empty_dirs(root: Path, start: Path) -> None:
    # called after deleting a file during sync, so an emptied-out hive partition directory doesn't linger behind
    current: Path = start.parent
    while current != root and current.exists() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


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
class BearpondClient:

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
    def __enter__(self) -> "BearpondClient":
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

    # ------------------------------------------------------------------------------------------------------------------
    def sync(self, target_dir: Path, dry_run: bool = False) -> None:
        manifest: types.Manifest = self.get_manifest()
        target_dir.mkdir(parents=True, exist_ok=True)

        manifest_files: dict[str, SyncedFileState] = {
            f.path: SyncedFileState(size=f.size, sha256=f.sha256) for f in manifest.files
        }
        local_state: dict[str, SyncedFileState] = load_local_state(target_dir)

        to_download: list = [p for p in manifest_files if local_state.get(p) != manifest_files[p]]
        to_delete: list = [p for p in local_state if p not in manifest_files]

        print(
            f"manifest-{manifest.seq:08d}: {len(manifest_files)} files "
            f"({len(to_download)} to fetch, {len(to_delete)} to prune)"
        )

        if dry_run:
            for p in to_download:
                print(f"  would fetch: {p}")
            for p in to_delete:
                print(f"  would prune: {p}")
        else:
            # state is saved after every single file so a crash or dropped connection mid-sync loses at most the file in flight
            state: dict[str, SyncedFileState] = dict(local_state)

            for rel_path in to_download:
                dest_path: Path = target_dir / rel_path
                expected: SyncedFileState = manifest_files[rel_path]
                actual_sha256: str = self.download_file(rel_path, dest_path)
                if actual_sha256 != expected.sha256:
                    dest_path.unlink(missing_ok=True)
                    raise BearpondError(f"checksum mismatch after fetching {rel_path}")
                state[rel_path] = SyncedFileState(size=expected.size, sha256=actual_sha256)
                save_local_state(target_dir, state)
                print(f"  fetched: {rel_path}")

            for rel_path in to_delete:
                dest_path = target_dir / rel_path
                dest_path.unlink(missing_ok=True)
                prune_empty_dirs(target_dir, dest_path)
                del state[rel_path]
                save_local_state(target_dir, state)
                print(f"  pruned: {rel_path}")

            print(
                f"Synced to manifest-{manifest.seq:08d}: "
                f"+{len(to_download)} -{len(to_delete)} (total {len(state)} files)"
            )

    # --- writes ------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def begin_transaction(self, added: list[types.FileMetadata], removed: list[types.FileMetadata] | None = None) -> str:
        body: types.TransactionManifest = types.TransactionManifest(added=added, removed=removed or [])
        response: httpx.Response = self._client.post("/transactions", json=body.model_dump(), headers=self._headers())
        self._raise_for_status(response)
        result: types.BeginTransactionResponse = types.BeginTransactionResponse.model_validate_json(
            response.text
        )
        return result.txn_id

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, txn_id: str) -> types.CommitResponse:
        response: httpx.Response = self._client.post(f"/transactions/{txn_id}/commit", headers=self._headers())
        self._raise_for_status(response)
        result: types.CommitResponse = types.CommitResponse.model_validate_json(response.text)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def abort(self, txn_id: str) -> None:
        response: httpx.Response = self._client.delete(f"/transactions/{txn_id}", headers=self._headers())
        self._raise_for_status(response)

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction_status(self, txn_id: str) -> types.TransactionStatus:
        # tells a caller whether a transaction is still open or already committed — the way to learn the outcome
        # of a commit whose response was lost (retrying commit() itself is also safe: it's idempotent)
        response: httpx.Response = self._client.get(f"/transactions/{txn_id}", headers=self._headers())
        self._raise_for_status(response)
        result: types.TransactionStatus = types.TransactionStatus.model_validate_json(response.text)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def upload_file(self, txn_id: str, rel_path: str, local_path: Path) -> types.FileMetadata:
        response: httpx.Response = self._client.put(
            f"/transactions/{txn_id}/files/{rel_path}", content=iter_file_chunks(local_path), headers=self._headers()
        )
        self._raise_for_status(response)
        result: types.FileMetadata = types.FileMetadata.model_validate_json(response.text)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def upload_files(self, txn_id: str, files: dict[str, Path]) -> list[types.FileMetadata]:
        # uploads into an already-open transaction; doesn't begin, commit, or abort it — that's the caller's own
        # call to make: begin_transaction() -> upload_files() -> commit() or abort()
        results: list[types.FileMetadata] = []
        for rel_path, local_path in files.items():
            results.append(self.upload_file(txn_id, rel_path, local_path))
        return results
