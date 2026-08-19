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
from . import paths

# the repository module is only needed for the type hint below — a real import would create a cycle, since
# repository.py imports this module to construct Transaction instances
if TYPE_CHECKING:
    from . import repository

# serializes commit + manifest regeneration so concurrent commits can't race on the manifest's sequence number
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
# from types.FileMeta, which is the wire-protocol shape and carries its own path field
class FileMeta(BaseModel):
    size: int
    sha256: str


# adapter to validate/serialize the dict[str, FileMeta] shape shared by _declared.json, _removed.json, and _uploaded.json
_file_meta_adapter: TypeAdapter[dict[str, FileMeta]] = TypeAdapter(dict[str, FileMeta])


# ======================================================================================================================
# written to manifests/_commit_journal.json after validation passes and before any file moves — the durable record
# of intent that lets recovery finish a commit interrupted by a crash; deleted once the commit is fully recorded
class CommitJournal(BaseModel):
    txn_id: str
    declared: dict[str, FileMeta]
    removed: dict[str, FileMeta]


# ======================================================================================================================
class Transaction:

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, txn_id: str, repo: "repository.Repository") -> None:
        self.txn_id: str = txn_id
        self.repo: "repository.Repository" = repo
        self.txn_dir: Path = repo.staging_root / txn_id

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
        paths.atomic_write(state_path, _file_meta_adapter.dump_json(state))

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
        paths.atomic_write(state_path, _file_meta_adapter.dump_json(state))

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
        paths.atomic_write(state_path, _file_meta_adapter.dump_json(state))

    # ------------------------------------------------------------------------------------------------------------------
    async def upload_file(self, rel_path: str, chunks: AsyncIterator[bytes]) -> types.FileMeta:
        # only files in the original transaction manifest can be uploaded
        declared: dict[str, FileMeta] = self.load_declared()
        if rel_path not in declared:
            raise TransactionValidationError(f"path was not declared when the transaction was opened: {rel_path}")

        expected_sha256: str = declared[rel_path].sha256

        # stage inside the transaction's own directory — nothing is visible to readers until commit()
        dest_path: Path = paths.safe_join(self.txn_dir, rel_path)
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
        paths.fsync_dir(dest_path.parent)

        # mark this path as uploaded so commit() can refuse if anything declared is still missing
        state: dict[str, FileMeta] = self.load_uploaded()
        state[rel_path] = FileMeta(size=size, sha256=checksum)
        self.save_uploaded(state)

        # echo back what was stored so the caller can confirm size and hash match
        return types.FileMeta(path=rel_path, size=size, sha256=checksum)

    # ------------------------------------------------------------------------------------------------------------------
    def _place_declared_files(self, declared: dict[str, FileMeta]) -> dict[str, tuple[os.stat_result, str]]:
        # moves every staged file into its permanent place in data/ — idempotent, so an interrupted attempt can
        # simply be run again: a file already moved is found in the lake instead of staged, and only a file missing
        # from both is unrecoverable. Returns (stat, sha256) per path for seeding the hash cache.
        hash_cache_entries: dict[str, tuple[os.stat_result, str]] = {}
        for rel_path, meta in declared.items():
            staged_path: Path = self.txn_dir / rel_path
            final_path: Path = paths.safe_join(self.repo.data_root, rel_path)

            if staged_path.is_file():
                if final_path.exists():
                    # commit validation already verified the lake copy is byte-identical — drop the redundant staged one
                    staged_path.unlink()
                else:
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_path, final_path)
            elif not final_path.is_file():
                raise TransactionConflictError(
                    f"interrupted commit cannot complete: {rel_path} is missing from both staging and the lake"
                )

            hash_cache_entries[rel_path] = (final_path.stat(), meta.sha256)
        return hash_cache_entries

    # ------------------------------------------------------------------------------------------------------------------
    def _move_removed_files(self, removed: dict[str, FileMeta]) -> None:
        # moves every removed file into removed/ rather than deleting it, so it stays recoverable — idempotent:
        # a file already moved by an interrupted attempt is simply gone from data/, and the postcondition
        # "no longer in the lake" already holds for it
        for rel_path in removed:
            source_path: Path = paths.safe_join(self.repo.data_root, rel_path)
            if source_path.is_file():
                removed_path: Path = paths.safe_join(self.repo.removed_root, rel_path)
                removed_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source_path, removed_path)

    # ------------------------------------------------------------------------------------------------------------------
    def _fsync_commit_dirs(self, journal: CommitJournal) -> None:
        # fsyncs every directory whose entries the moves changed — fsyncing a moved file doesn't make its rename
        # durable, only fsyncing the directories does
        dirs: set[Path] = {self.txn_dir}
        for rel_path in journal.declared:
            dirs.add((self.txn_dir / rel_path).parent)
            dirs.add(paths.safe_join(self.repo.data_root, rel_path).parent)
        for rel_path in journal.removed:
            dirs.add(paths.safe_join(self.repo.data_root, rel_path).parent)
            dirs.add(paths.safe_join(self.repo.removed_root, rel_path).parent)
        for directory in dirs:
            if directory.is_dir():
                paths.fsync_dir(directory)

    # ------------------------------------------------------------------------------------------------------------------
    def _execute(self, journal: CommitJournal) -> types.Manifest:
        # the crash-safe half of a commit, shared by commit() and recovery replay: once the journal is on disk,
        # every step here is idempotent, so the commit can always be driven to completion by running them again
        hash_cache_entries: dict[str, tuple[os.stat_result, str]] = self._place_declared_files(journal.declared)

        # seed the hash cache with what upload already verified, so the manifest regen below won't re-hash any of it
        self.repo.seed_hash_cache(hash_cache_entries)

        self._move_removed_files(journal.removed)

        # make the moves themselves durable before the journal can come down — the journal is the guarantee that
        # unfinished moves get replayed, so it must outlast any possibility that the moves survive without it
        self._fsync_commit_dirs(journal)

        # record the commit, which also regenerates the manifest so it reflects the files that just landed
        manifest: types.Manifest = self.repo.record_commit(
            txn_id=journal.txn_id,
            new_files=list(journal.declared.keys()),
            removed_files=list(journal.removed.keys()),
        )

        # the staging directory has served its purpose
        if self.txn_dir.exists():
            shutil.rmtree(self.txn_dir)
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def replay(self, journal: CommitJournal) -> None:
        # recovery entry point for a commit interrupted after its journal was written — validation is deliberately
        # not re-run: the commit was accepted once, so replay finishes it unconditionally
        self._execute(journal)

    # ------------------------------------------------------------------------------------------------------------------
    def commit(self) -> types.CommitResponse:
        declared: dict[str, FileMeta] = self.load_declared()
        uploaded: dict[str, FileMeta] = self.load_uploaded()
        removed: dict[str, FileMeta] = self.load_removed()

        # refuse to commit while any declared file hasn't actually been uploaded yet
        missing: set = set(declared) - set(uploaded)
        if missing:
            raise TransactionValidationError(f"declared files not yet uploaded: {sorted(missing)}")

        with _commit_lock:
            # re-check now since disk state can change between open and commit — this is the real guarantee, the open-time check was just fail-fast
            for rel_path, meta in declared.items():
                staged_path: Path = self.txn_dir / rel_path
                if not staged_path.is_file():
                    raise TransactionConflictError(f"staged file missing: {rel_path}")
                self.repo.check_no_unsafe_collision(rel_path, meta.sha256)

            # a file queued for removal may have changed or vanished due to another commit since this transaction opened
            for rel_path, meta in removed.items():
                if not paths.safe_join(self.repo.data_root, rel_path).is_file():
                    raise TransactionConflictError(f"path to remove no longer exists: {rel_path}")
                self.repo.check_safe_to_remove(rel_path, meta.sha256)

            # the commit point: once the journal is on disk this commit is guaranteed to complete — either here, or
            # in recovery at next startup. The commit lock guarantees at most one journal exists at a time.
            journal: CommitJournal = CommitJournal(txn_id=self.txn_id, declared=declared, removed=removed)
            self.repo.write_journal(journal)
            manifest: types.Manifest = self._execute(journal)
            self.repo.delete_journal()

        # report the manifest version this commit produced and how many files were added/removed
        return types.CommitResponse(
            seq=manifest.seq,
            files_added_count=len(declared),
            files_removed_count=len(removed),
        )

    # ------------------------------------------------------------------------------------------------------------------
    def abort(self) -> None:
        shutil.rmtree(self.txn_dir)
