import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from . import config
from . import paths
from . import transaction
from .. import types

# a hive partition directory segment, e.g. "year=2024" — key=value, no slashes or extra '=' in either side
_HIVE_PARTITION_SEGMENT: re.Pattern = re.compile(r"^\w+=[^/=]+$")


# ======================================================================================================================
class RepositoryError(Exception):
    pass


# ======================================================================================================================
# one CommitRecord is written per commit, under commits/{txn_id}.json, and kept forever — never pruned or overwritten —
# so the lake's full history of how it reached its current state stays reconstructable
class CommitRecord(BaseModel):
    txn_id: str
    seq: int
    committed_at: str
    new_files: list[str]
    removed_files: list[str]
    reason: str | None


# ======================================================================================================================
class Repository:
    COMMITS_DIR_NAME = "commits"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        # every metadata write in the repository (journal, manifests, _latest pointer, commit records) funnels
        # through here, so this one call is what makes all of them atomic and power-loss-durable
        paths.atomic_write(path, content.encode())

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _validate_hive_parquet_path(rel_path: str) -> None:
        if not rel_path.endswith(".parquet"):
            raise transaction.TransactionValidationError(f"path is not a parquet file: {rel_path}")

        # every directory segment before the filename must be a hive partition key=value pair
        segments: list[str] = rel_path.split("/")
        for segment in segments[:-1]:
            if not _HIVE_PARTITION_SEGMENT.match(segment):
                raise transaction.TransactionValidationError(
                    f"path segment is not a valid hive partition (expected key=value): {segment!r} in {rel_path}"
                )

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, server_config: config.ServerConfig) -> None:
        # the repository's entire on-disk layout: committed files, in-flight uploads, removed files, and manifest state
        self.data_root: Path = server_config.repo_root / "data"
        self.staging_root: Path = server_config.repo_root / "staging"
        self.removed_root: Path = server_config.repo_root / "removed"
        self.manifest_root: Path = server_config.repo_root / "manifests"
        self.hash_cache_path: Path = self.manifest_root / ".hash_cache.sqlite"
        self.latest_pointer: Path = self.manifest_root / "_latest"
        self.journal_path: Path = self.manifest_root / "_commit_journal.json"

    # ------------------------------------------------------------------------------------------------------------------
    def _open_cache(self) -> sqlite3.Connection:
        self.hash_cache_path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection = sqlite3.connect(self.hash_cache_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hash_cache ("
            "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT)"
        )
        return conn

    # ------------------------------------------------------------------------------------------------------------------
    def _hash_with_cache(self, conn: sqlite3.Connection, path: Path, rel_path: str) -> str:
        # mtime+size match means the file wasn't touched since we last hashed it
        stat: os.stat_result = path.stat()
        row: tuple | None = conn.execute(
            "SELECT size, mtime_ns, sha256 FROM hash_cache WHERE path = ?", (rel_path,)
        ).fetchone()

        digest: str
        if row and row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
            digest = row[2]
        else:
            digest = paths.sha256_file(path)
            conn.execute(
                "INSERT OR REPLACE INTO hash_cache (path, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
                (rel_path, stat.st_size, stat.st_mtime_ns, digest),
            )
        return digest

    # ------------------------------------------------------------------------------------------------------------------
    def seed_hash_cache(self, entries: dict[str, tuple[os.stat_result, str]]) -> None:
        # lets a caller who already verified a file's hash (e.g. during upload) skip re-hashing it on the next commit
        conn: sqlite3.Connection = self._open_cache()
        for rel_path, (stat, sha256) in entries.items():
            conn.execute(
                "INSERT OR REPLACE INTO hash_cache (path, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
                (rel_path, stat.st_size, stat.st_mtime_ns, sha256),
            )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------------------------------------------------------
    def latest(self) -> types.Manifest | None:
        manifest: types.Manifest | None = None
        if self.latest_pointer.exists():
            manifest_name: str = self.latest_pointer.read_text().strip()
            manifest_path: Path = self.manifest_root / manifest_name
            if not manifest_path.exists():
                # a pointer naming a missing manifest means manifests/ is corrupt — that's a loud server error,
                # not an "empty lake" that a sync client would prune its entire local mirror against
                raise RepositoryError(f"manifest pointer is dangling: {self.latest_pointer} names missing {manifest_name}")
            try:
                manifest = types.Manifest.model_validate_json(manifest_path.read_text())
            except ValidationError as e:
                raise RepositoryError(f"manifest file is corrupt: {manifest_path}") from e
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def _generate(self) -> types.Manifest:
        if not self.data_root.exists():
            raise RepositoryError(f"data root not found: {self.data_root}")

        # scan the lake and hash every file, reusing cached hashes for anything unchanged since last time
        conn: sqlite3.Connection = self._open_cache()
        parquet_files: list[Path] = sorted(self.data_root.rglob("*.parquet"))
        files: list[types.FileMeta] = []
        for path in parquet_files:
            rel_path: str = path.relative_to(self.data_root).as_posix()
            files.append(types.FileMeta(
                path=rel_path,
                size=path.stat().st_size,
                sha256=self._hash_with_cache(conn, path, rel_path),
            ))
        conn.commit()
        conn.close()

        # only write a new manifest if the scan actually differs from the last one — otherwise the current one still stands
        previous: types.Manifest | None = self.latest()
        previous_files: dict[str, tuple[int, str]] = (
            {f.path: (f.size, f.sha256) for f in previous.files} if previous else {}
        )
        current_files: dict[str, tuple[int, str]] = {f.path: (f.size, f.sha256) for f in files}

        manifest: types.Manifest
        if previous is not None and previous_files == current_files:
            manifest = previous
        else:
            seq: int = (previous.seq + 1) if previous else 1
            manifest = types.Manifest(seq=seq, created_at=datetime.now(timezone.utc).isoformat(), files=files)
            self.manifest_root.mkdir(parents=True, exist_ok=True)
            manifest_name: str = f"manifest-{seq:08d}.json"
            self._atomic_write(self.manifest_root / manifest_name, manifest.model_dump_json(indent=2))
            self._atomic_write(self.latest_pointer, manifest_name)

        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def _commits_dir(self) -> Path:
        return self.manifest_root / self.COMMITS_DIR_NAME

    # ------------------------------------------------------------------------------------------------------------------
    def load_commit_record(self, txn_id: str) -> CommitRecord | None:
        record: CommitRecord | None = None
        record_path: Path = self._commits_dir() / f"{txn_id}.json"
        if record_path.exists():
            record = CommitRecord.model_validate_json(record_path.read_text())
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def record_commit(
        self,
        txn_id: str,
        new_files: list[str],
        removed_files: list[str] | None = None,
        reason: str | None = None,
    ) -> types.Manifest:
        record_path: Path = self._commits_dir() / f"{txn_id}.json"

        manifest: types.Manifest
        if record_path.exists():
            # this commit was already recorded before a crash interrupted its surrounding cleanup — replaying it
            # must neither double-record nor regenerate the manifest, which already reflects the commit
            existing: types.Manifest | None = self.latest()
            if existing is None:
                raise RepositoryError(f"commit record exists but no manifest does: {record_path}")
            manifest = existing
        else:
            # a commit always leaves the lake's current-state snapshot up to date — there's no such thing as a
            # commit that isn't reflected in the manifest, so generating one is simply part of recording the commit
            manifest = self._generate()

            record: CommitRecord = CommitRecord(
                txn_id=txn_id,
                seq=manifest.seq,
                committed_at=datetime.now(timezone.utc).isoformat(),
                new_files=new_files,
                removed_files=removed_files or [],
                reason=reason,
            )
            target_dir: Path = self._commits_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(record_path, record.model_dump_json(indent=2))

        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def write_journal(self, journal: transaction.CommitJournal) -> None:
        # durable record of a commit's intent, written before any file moves — at most one exists at a time,
        # guaranteed by the commit lock in transaction.py
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.journal_path, journal.model_dump_json(indent=2))

    # ------------------------------------------------------------------------------------------------------------------
    def load_journal(self) -> transaction.CommitJournal | None:
        journal: transaction.CommitJournal | None = None
        if self.journal_path.exists():
            journal = transaction.CommitJournal.model_validate_json(self.journal_path.read_text())
        return journal

    # ------------------------------------------------------------------------------------------------------------------
    def delete_journal(self) -> None:
        self.journal_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------------------------------------------------------
    def recover(self) -> None:
        # runs once at startup, before traffic is served: finishes any commit interrupted after its journal was
        # written, so a crash can never leave the lake halfway between two manifest versions. No lock needed —
        # nothing else is running yet. A leftover journal is kept (and startup fails) if the commit can't finish.
        journal: transaction.CommitJournal | None = self.load_journal()
        if journal is not None:
            interrupted: transaction.Transaction = transaction.Transaction(journal.txn_id, self)
            try:
                interrupted.replay(journal)
            except transaction.TransactionError as e:
                raise RepositoryError(f"recovery could not finish interrupted commit {journal.txn_id}: {e}") from e
            self.delete_journal()

    # ------------------------------------------------------------------------------------------------------------------
    def check_no_unsafe_collision(self, rel_path: str, expected_sha256: str) -> None:
        # a path that already exists in the lake is only acceptable if its content is byte-identical to what's declared
        final_path: Path = paths.safe_join(self.data_root, rel_path)
        if final_path.exists() and paths.sha256_file(final_path) != expected_sha256:
            raise transaction.TransactionConflictError(
                f"{rel_path} already exists in the lake with different content — "
                f"modifying an existing file is only allowed via compaction"
            )

    # ------------------------------------------------------------------------------------------------------------------
    def check_safe_to_remove(self, rel_path: str, expected_sha256: str) -> None:
        # a path queued for removal is only safe to remove if it still matches what the client observed when it
        # decided to remove it — if the content differs, another transaction changed it in the meantime
        final_path: Path = paths.safe_join(self.data_root, rel_path)
        if final_path.is_file() and paths.sha256_file(final_path) != expected_sha256:
            raise transaction.TransactionConflictError(
                f"{rel_path} has changed since it was declared for removal — remove is only safe against the "
                f"exact content that was observed"
            )

    # ------------------------------------------------------------------------------------------------------------------
    def begin_transaction(self, manifest: types.TransactionManifest) -> transaction.Transaction:
        # a transaction that neither adds nor removes anything isn't meaningful
        if not manifest.added and not manifest.removed:
            raise transaction.TransactionValidationError("transaction must add or remove at least one file")

        # build the declared set, rejecting a path named more than once in the same manifest
        declared: dict[str, transaction.FileMeta] = {}
        for declared_file in manifest.added:
            self._validate_hive_parquet_path(declared_file.path)
            if declared_file.path in declared:
                raise transaction.TransactionValidationError(f"path declared more than once: {declared_file.path}")
            declared[declared_file.path] = transaction.FileMeta(size=declared_file.size, sha256=declared_file.sha256)

        # build the removed set, rejecting a path named more than once in the same manifest
        removed: dict[str, transaction.FileMeta] = {}
        for removed_file in manifest.removed:
            if removed_file.path in removed:
                raise transaction.TransactionValidationError(f"path removed more than once: {removed_file.path}")
            removed[removed_file.path] = transaction.FileMeta(size=removed_file.size, sha256=removed_file.sha256)

        # a path can't be both a new file this transaction adds and one it removes
        overlap: set = set(declared) & set(removed)
        if overlap:
            raise transaction.TransactionValidationError(f"paths cannot be both added and removed: {sorted(overlap)}")

        # a removed path has to actually exist, with the exact content the client observed, for there to be
        # anything to remove
        for rel_path, meta in removed.items():
            if not paths.safe_join(self.data_root, rel_path).is_file():
                raise transaction.TransactionValidationError(
                    f"cannot remove a path that doesn't exist in the lake: {rel_path}"
                )
            self.check_safe_to_remove(rel_path, meta.sha256)

        # fail before creating anything — catches path collisions before a client uploads a single byte
        for rel_path, meta in declared.items():
            self.check_no_unsafe_collision(rel_path, meta.sha256)

        # only create the transaction's staging directory once everything above has passed validation
        txn: transaction.Transaction = transaction.Transaction(uuid.uuid4().hex, self)
        txn.txn_dir.mkdir(parents=True)
        txn.save_declared(declared)
        txn.save_removed(removed)
        return txn

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_id: str) -> transaction.Transaction:
        txn: transaction.Transaction = transaction.Transaction(txn_id, self)
        if not txn.txn_dir.is_dir():
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_id}")
        return txn


# the server process has exactly one repository, configured once at startup — everything else reaches it through
# get_repository() rather than constructing or holding its own Repository instance
_instance: Repository | None = None


# ----------------------------------------------------------------------------------------------------------------------
def configure(server_config: config.ServerConfig) -> None:
    global _instance
    _instance = Repository(server_config)


# ----------------------------------------------------------------------------------------------------------------------
def get_repository() -> Repository:
    if _instance is None:
        raise RuntimeError("lakesync server is not configured — set LAKESYNC_CONFIG_DIR before startup")
    return _instance


# auto-configure from LAKESYNC_CONFIG_DIR so importing this module is enough to get a working singleton in the
# server process — tests call configure() directly instead and don't need the env var set
_env_config_dir: Path | None = config.config_dir_from_env()
if _env_config_dir is not None:
    configure(config.load_server_config(_env_config_dir))
