import hashlib
import json
import os
import shutil
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from .. import types
from . import metadata_store
from . import object_store
from . import utils

# the repository module is only needed for the type hint below — a real import would create a cycle, since
# repository.py imports this module to construct Transaction instances
if TYPE_CHECKING:
    from . import repository

# serializes commits so object placement, the metadata commit, and the export never interleave between transactions
_commit_lock: threading.Lock = threading.Lock()


# ======================================================================================================================
class TransactionError(Exception):
    pass


# ======================================================================================================================
class TransactionNotFoundError(TransactionError):
    pass


# ======================================================================================================================
class TransactionValidationError(TransactionError):
    pass


# ======================================================================================================================
class TransactionConflictError(TransactionError):
    pass


# ======================================================================================================================
# size/sha256 for one path, keyed externally by path in _declared.json / _removed.json / _uploaded.json — distinct
# from types.FileMetadata, which is the wire-protocol shape and carries its own path field
class FileMeta(BaseModel):
    size: int
    sha256: str


# adapter to validate/serialize the dict[str, FileMeta] shape shared by _declared.json, _removed.json, and _uploaded.json
_file_meta_adapter: TypeAdapter[dict[str, FileMeta]] = TypeAdapter(dict[str, FileMeta])


# ======================================================================================================================
class Transaction:

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, txn_uuid: str, repo: "repository.Repository") -> None:
        self.txn_uuid: str = txn_uuid
        self.repo: "repository.Repository" = repo
        self.txn_dir: Path = repo.staging_root / txn_uuid

    # ------------------------------------------------------------------------------------------------------------------
    def load_declared(self) -> dict[str, FileMeta]:
        state_path: Path = self.txn_dir / "_declared.json"
        state: dict[str, FileMeta] = {}
        if state_path.exists():
            state = _file_meta_adapter.validate_json(state_path.read_text())
        return state

    # ------------------------------------------------------------------------------------------------------------------
    def save_declared(self, state: dict[str, FileMeta]) -> None:
        state_path: Path = self.txn_dir / "_declared.json"
        utils.atomic_write(state_path, _file_meta_adapter.dump_json(state))

    # ------------------------------------------------------------------------------------------------------------------
    def load_removed(self) -> dict[str, FileMeta]:
        state_path: Path = self.txn_dir / "_removed.json"
        state: dict[str, FileMeta] = {}
        if state_path.exists():
            state = _file_meta_adapter.validate_json(state_path.read_text())
        return state

    # ------------------------------------------------------------------------------------------------------------------
    def save_removed(self, state: dict[str, FileMeta]) -> None:
        state_path: Path = self.txn_dir / "_removed.json"
        utils.atomic_write(state_path, _file_meta_adapter.dump_json(state))

    # ------------------------------------------------------------------------------------------------------------------
    def load_uploaded(self) -> dict[str, FileMeta]:
        state_path: Path = self.txn_dir / "_uploaded.json"
        state: dict[str, FileMeta] = {}
        if state_path.exists():
            state = _file_meta_adapter.validate_json(state_path.read_text())
        return state

    # ------------------------------------------------------------------------------------------------------------------
    def save_uploaded(self, state: dict[str, FileMeta]) -> None:
        state_path: Path = self.txn_dir / "_uploaded.json"
        utils.atomic_write(state_path, _file_meta_adapter.dump_json(state))

    # ------------------------------------------------------------------------------------------------------------------
    async def upload_file(self, rel_path: str, chunks: AsyncIterator[bytes]) -> types.FileMetadata:
        # only files in the original transaction manifest can be uploaded
        declared: dict[str, FileMeta] = self.load_declared()
        if rel_path not in declared:
            raise TransactionValidationError(f"path was not declared when the transaction was opened: {rel_path}")

        expected_sha256: str = declared[rel_path].sha256

        # stage inside the transaction's own directory — nothing is visible to readers until commit()
        dest_path: Path = utils.safe_join(self.txn_dir, rel_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path = dest_path.with_suffix(dest_path.suffix + ".tmp")

        # stream the body to disk while hashing it chunk by chunk — files can be gigabytes, never buffer the whole thing
        digest: hashlib._Hash = hashlib.sha256()
        size: int = 0
        with tmp_path.open("wb") as f:
            async for chunk in chunks:
                digest.update(chunk)
                f.write(chunk)
                size += len(chunk)
            # flush before the rename so an acknowledged upload is a durable upload — commit later trusts staged
            # files by presence alone, so a staged file must never be one whose content is still only in RAM
            f.flush()
            os.fsync(f.fileno())
        checksum: str = digest.hexdigest()

        # reject and delete the file if what arrived doesn't match the hash declared up front
        if checksum != expected_sha256:
            tmp_path.unlink(missing_ok=True)
            raise TransactionValidationError(
                f"sha256 mismatch against declared value for {rel_path}: expected {expected_sha256}, got {checksum}"
            )

        # atomic rename into place so nothing ever observes a half-written file, then make the rename itself durable
        os.replace(tmp_path, dest_path)
        utils.fsync_dir(dest_path.parent)

        # mark this path as uploaded so commit() can refuse if anything declared is still missing
        state: dict[str, FileMeta] = self.load_uploaded()
        state[rel_path] = FileMeta(size=size, sha256=checksum)
        self.save_uploaded(state)

        # echo back what was stored so the caller can confirm size and hash match
        return types.FileMetadata(path=rel_path, size=size, sha256=checksum)

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self, reason: str | None = None) -> types.CommitResponse:
        declared: dict[str, FileMeta] = self.load_declared()
        uploaded: dict[str, FileMeta] = self.load_uploaded()
        removed: dict[str, FileMeta] = self.load_removed()

        # refuse to commit while any declared file hasn't actually been uploaded yet
        missing: set = set(declared) - set(uploaded)
        if missing:
            raise TransactionValidationError(f"declared files not yet uploaded: {sorted(missing)}")

        with _commit_lock:
            # content before pointers: objects are placed first, and placement is idempotent — the name is the
            # hash, so a retry after a crash simply finds the object already stored. A crash here leaves orphans
            # for a future GC, never an inconsistency.
            for rel_path, meta in declared.items():
                try:
                    self.repo.object_store.insert(meta.sha256, self.txn_dir / rel_path)
                except object_store.ObjectStoreError as e:
                    raise TransactionConflictError(f"cannot place {rel_path}: {e}") from e

            # the commit point: one atomic metadata transaction flips every pointer — collision and removal
            # checks happen inside it, so check-and-act can never race. Idempotent by txn_uuid, so a client that
            # lost the response can safely retry.
            record: metadata_store.CommitRecord = self.repo.record_commit(self.txn_uuid, declared, removed, reason=reason)

            # the staging directory has served its purpose
            if self.txn_dir.exists():
                shutil.rmtree(self.txn_dir)

        # report the manifest version this commit produced and how many files were added/removed
        return types.CommitResponse(
            seq=record.seq,
            files_added_count=len(declared),
            files_removed_count=len(removed),
        )

    # ------------------------------------------------------------------------------------------------------------------
    def abort(self) -> None:
        shutil.rmtree(self.txn_dir)
