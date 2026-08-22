import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, TypeAdapter

from .. import types

# validates/serializes the added/removed FileMetadata lists stored as JSON text in the commits table
_file_meta_list_adapter: TypeAdapter[list[types.FileMetadata]] = TypeAdapter(list[types.FileMetadata])

# validates/serializes the updated UpdatedFileMetadata list stored as JSON text in the commits table
_updated_meta_list_adapter: TypeAdapter[list[types.UpdatedFileMetadata]] = TypeAdapter(list[types.UpdatedFileMetadata])


# ======================================================================================================================
class MetadataStoreError(Exception):
    pass


# ======================================================================================================================
class MetadataConflictError(MetadataStoreError):
    # a commit's declared changes no longer match the current mapping — another commit landed in the meantime
    pass


# ----------------------------------------------------------------------------------------------------------------------
def compute_commit_hash(
    parent_commit_hash: str | None,
    seq: int,
    committed_at: str,
    added: list[types.FileMetadata],
    removed: list[types.FileMetadata],
    updated: list[types.UpdatedFileMetadata],
    user: str | None,
    reason: str | None,
) -> str:
    # fixed field order and compact separators make the hash reproducible from the exported record alone.
    # added/removed/updated are sorted by path before serializing — they're semantically sets, and the hash must
    # depend only on the change's content, never on list order (same reason git stores tree entries sorted by name)
    payload: dict = {
        "parent_commit_hash": parent_commit_hash,
        "seq": seq,
        "committed_at": committed_at,
        "added": [f.model_dump() for f in sorted(added, key=lambda f: f.path)],
        "removed": [f.model_dump() for f in sorted(removed, key=lambda f: f.path)],
        "updated": [f.model_dump() for f in sorted(updated, key=lambda f: f.path)],
        "user": user,
        "reason": reason,
    }
    canonical: str = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ======================================================================================================================
# one page of a directory listing: the files directly under a prefix and the distinct immediate subdirectories
# below it. next_cursor is set when the file page is truncated — pass it back as cursor to get the next page.
class DirectoryPage(BaseModel):
    files: list[types.FileMetadata]
    directories: list[str]
    next_cursor: str | None


# ======================================================================================================================
class MetadataStore(Protocol):

    # the lake's system of record: which content lives at which path, the full history of how it got there, and
    # the manifest sequence. commit_changes is the commit point of a transaction — implementations must make it
    # atomic (all checks and updates in one storage transaction) and idempotent by txn_uuid (a retry after a lost
    # response returns the original record).

    # ------------------------------------------------------------------------------------------------------------------
    def get_file_metadata(self, path: str) -> types.FileMetadata | None: ...

    # ------------------------------------------------------------------------------------------------------------------
    def list_directory(self, prefix: str, limit: int, cursor: str | None = None) -> "DirectoryPage": ...

    # ------------------------------------------------------------------------------------------------------------------
    def get_current_seq(self) -> int: ...

    # ------------------------------------------------------------------------------------------------------------------
    def get_manifest(self, seq: int) -> types.Manifest | None: ...

    # ------------------------------------------------------------------------------------------------------------------
    def commit_changes(
        self,
        txn_uuid: str,
        added: list[types.FileMetadata],
        removed: list[types.FileMetadata],
        updated: list[types.UpdatedFileMetadata],
        user: str | None = None,
        reason: str | None = None,
    ) -> types.CommitRecord: ...

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record(self, txn_uuid: str) -> types.CommitRecord | None: ...

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_list(self) -> list[types.CommitRecord]: ...

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_page(self, limit: int, before_seq: int | None = None) -> types.CommitHistoryPage: ...

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None: ...


# ======================================================================================================================
class SqliteMetadataStore(MetadataStore):

    # one table of record: every path ever added, with the txn/seq that added it and — once removed — the txn/seq
    # that removed it. The lake's current state is simply the live rows (removed_seq IS NULL); there is no separate
    # "current" table to drift out of sync with this one.
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS objects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        added_txn TEXT NOT NULL,
        added_seq INTEGER NOT NULL,
        removed_txn TEXT,
        removed_seq INTEGER
    );
    -- at most one live row per path — the invariant a separate current table's primary key would otherwise enforce
    CREATE UNIQUE INDEX IF NOT EXISTS objects_live_path ON objects(path) WHERE removed_seq IS NULL;
    
    CREATE TABLE IF NOT EXISTS commits (
        commit_hash TEXT NOT NULL UNIQUE,
        parent_commit_hash TEXT,
        txn_uuid TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        committed_at TEXT NOT NULL,
        added TEXT NOT NULL,
        removed TEXT NOT NULL,
        updated TEXT NOT NULL,
        user TEXT,
        reason TEXT
    );
    """

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _row_to_commit_record(row: tuple) -> types.CommitRecord:
        # row columns arrive in the same order the model declares them: commit_hash, parent_commit_hash, txn_uuid,
        # seq, committed_at, added, removed, updated, user, reason
        return types.CommitRecord(
            commit_hash=row[0],
            parent_commit_hash=row[1],
            txn_uuid=row[2],
            seq=row[3],
            committed_at=row[4],
            added=_file_meta_list_adapter.validate_json(row[5]),
            removed=_file_meta_list_adapter.validate_json(row[6]),
            updated=_updated_meta_list_adapter.validate_json(row[7]),
            user=row[8],
            reason=row[9],
        )

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # create the connection. check_same_thread=False bc we will have different threads.
        self._conn: sqlite3.Connection = sqlite3.connect(db_path, check_same_thread=False)
        
        # re-entrant, so methods can call each other (commit_changes → get_commit_record) without self-deadlock
        self._lock: threading.RLock = threading.RLock()
        
        # we need to synchronize execution of the initial schema creation
        with self._lock:
            self._conn.executescript(self._SCHEMA)
            self._migrate()
            self._conn.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def _migrate(self) -> None:
        # older databases may be missing columns added after the initial schema; ALTER TABLE brings them in line
        with self._lock:
            columns: list[tuple] = self._conn.execute("PRAGMA table_info(commits)").fetchall()
            if not any(col[1] == "updated" for col in columns):
                self._conn.execute("ALTER TABLE commits ADD COLUMN updated TEXT NOT NULL DEFAULT '[]'")
                self._conn.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def get_file_metadata(self, path: str) -> types.FileMetadata | None:
        with self._lock:
            row: tuple | None = self._conn.execute(
                "SELECT size, sha256 FROM objects WHERE path = ? AND removed_seq IS NULL", (path,)
            ).fetchone()
        result: types.FileMetadata | None = None
        if row is not None:
            result = types.FileMetadata(path=path, size=row[0], sha256=row[1])
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def list_directory(self, prefix: str, limit: int, cursor: str | None = None) -> "DirectoryPage":
        # S3-style prefix+delimiter ("/") listing over the live mapping: files directly under the prefix, and the
        # distinct immediate subdirectories rolled up in sqlite (the standard's Contents + CommonPrefixes).
        # GLOB rather than LIKE — LIKE is case-insensitive for ASCII in sqlite (wrong for paths), and a
        # literal-prefix GLOB range-scans the objects_live_path index. Files paginate by key (cursor = the last
        # file's full path from a previous page); directories are returned in full — they're few by construction.
        glob_prefix: str = prefix + "*"
        rest_start: int = len(prefix) + 1  # sqlite substr is 1-based
        with self._lock:
            # one row beyond the limit is how we know the page is truncated
            file_rows: list[tuple] = self._conn.execute(
                "SELECT path, size, sha256 FROM objects "
                "WHERE removed_seq IS NULL AND path GLOB ? AND instr(substr(path, ?), '/') = 0 AND path > ? "
                "ORDER BY path LIMIT ?",
                (glob_prefix, rest_start, cursor or "", limit + 1),
            ).fetchall()
            dir_rows: list[tuple] = self._conn.execute(
                "SELECT DISTINCT substr(path, 1, ? + instr(substr(path, ?), '/') - 1) AS dir FROM objects "
                "WHERE removed_seq IS NULL AND path GLOB ? AND instr(substr(path, ?), '/') > 0 ORDER BY dir",
                (len(prefix), rest_start, glob_prefix, rest_start),
            ).fetchall()

        truncated: bool = len(file_rows) > limit
        page_rows: list[tuple] = file_rows[:limit]
        return DirectoryPage(
            files=[types.FileMetadata(path=row[0], size=row[1], sha256=row[2]) for row in page_rows],
            directories=[row[0] for row in dir_rows],
            next_cursor=page_rows[-1][0] if truncated and page_rows else None,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def get_current_seq(self) -> int:
        with self._lock:
            # COALESCE covers the empty commits table: an aggregate query always returns one row, MAX of zero rows
            # is NULL, and 0 is the lake's "no commits yet" sentinel — the first commit gets seq 1
            seq: int = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM commits").fetchone()[0]
        return seq

    # ------------------------------------------------------------------------------------------------------------------
    def get_manifest(self, seq: int) -> types.Manifest | None:
        # the lake as of the given seq: everything added at or before it and not yet removed by then. Returns
        # None for a seq with no commit record (0 — the never-committed state — or beyond the current seq).
        with self._lock:
            # every recorded commit advances the seq exactly once, so each seq has exactly one commit record
            row: tuple | None = self._conn.execute(
                "SELECT committed_at FROM commits WHERE seq = ?", (seq,)
            ).fetchone()

            # if there is a commit with the given seq
            manifest: types.Manifest | None = None
            if row is not None:
                # select all the files that were added before or by seq and have been deleted (yet)
                rows: list[tuple] = self._conn.execute(
                    "SELECT path, size, sha256 FROM objects "
                    "WHERE added_seq <= ? AND (removed_seq IS NULL OR removed_seq > ?) ORDER BY path",
                    (seq, seq),
                ).fetchall()
                
                # creat the Manifest with the selected FileMetadata
                manifest = types.Manifest(
                    seq=seq,
                    created_at=row[0],
                    files=[types.FileMetadata(path=r[0], size=r[1], sha256=r[2]) for r in rows],
                )
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record(self, txn_uuid: str) -> types.CommitRecord | None:
        with self._lock:
            row: tuple | None = self._conn.execute(
                "SELECT commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, added, removed, updated, user, reason "
                "FROM commits WHERE txn_uuid = ?",
                (txn_uuid,),
            ).fetchone()
        record: types.CommitRecord | None = None
        if row is not None:
            record = self._row_to_commit_record(row)
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def commit_changes(
        self,
        txn_uuid: str,
        added: list[types.FileMetadata],
        removed: list[types.FileMetadata],
        updated: list[types.UpdatedFileMetadata],
        user: str | None = None,
        reason: str | None = None,
    ) -> types.CommitRecord:
        record: types.CommitRecord
        with self._lock:
            # a commit that already recorded itself is a retry after a lost response — answer with the original record
            existing: types.CommitRecord | None = self.get_commit_record(txn_uuid)
            if existing is not None:
                record = existing
            else:
                committed_at: str = datetime.now(timezone.utc).isoformat()
                
                # BEGIN IMMEDIATE takes the write lock up front, so the checks below and the updates they guard are
                # one atomic unit — a concurrent commit can't slip between check and act
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    current: dict[str, types.FileMetadata] = {
                        row[0]: types.FileMetadata(path=row[0], size=row[1], sha256=row[2])
                        for row in self._conn.execute(
                            "SELECT path, size, sha256 FROM objects WHERE removed_seq IS NULL"
                        ).fetchall()
                    }

                    # a path can only be in one of added, removed, or updated within a single commit
                    added_paths: set[str] = {f.path for f in added}
                    removed_paths: set[str] = {f.path for f in removed}
                    updated_paths: set[str] = {f.path for f in updated}
                    for path in added_paths & removed_paths:
                        raise MetadataConflictError(f"path cannot be both added and removed: {path}")
                    for path in added_paths & updated_paths:
                        raise MetadataConflictError(f"path cannot be both added and updated: {path}")
                    for path in removed_paths & updated_paths:
                        raise MetadataConflictError(f"path cannot be both removed and updated: {path}")

                    # an added path must not be live at all — different content requires a remove first, identical
                    # content is a no-op, and both are rejected: every recorded commit changes the lake by construction
                    for added_file in added:
                        if added_file.path in current:
                            if current[added_file.path].sha256 != added_file.sha256:
                                raise MetadataConflictError(
                                    f"{added_file.path} already exists in the lake with different content — "
                                    f"remove it before adding new content"
                                )
                            raise MetadataConflictError(
                                f"{added_file.path} already exists in the lake with identical content — "
                                f"re-adding it is a no-op"
                            )

                    # a removal is only safe against the exact content the client observed when it decided to remove
                    for removed_file in removed:
                        if removed_file.path not in current:
                            raise MetadataConflictError(f"path to remove no longer exists: {removed_file.path}")
                        if current[removed_file.path].sha256 != removed_file.sha256:
                            raise MetadataConflictError(
                                f"{removed_file.path} has changed since it was declared for removal — "
                                f"remove is only safe against the exact content that was observed"
                            )

                    # an update is only safe against the exact content the client observed, and the new content must
                    # actually be different — otherwise the commit would be a no-op
                    for updated_file in updated:
                        if updated_file.path not in current:
                            raise MetadataConflictError(f"path to update no longer exists: {updated_file.path}")
                        if current[updated_file.path].sha256 != updated_file.old_sha256:
                            raise MetadataConflictError(
                                f"{updated_file.path} has changed since it was declared for update — "
                                f"update is only safe against the exact content that was observed"
                            )
                        if updated_file.old_sha256 == updated_file.new_sha256:
                            raise MetadataConflictError(
                                f"{updated_file.path} old and new content are identical — updating it is a no-op"
                            )

                    # the checks above guarantee this commit changes the mapping, so the seq always advances
                    seq: int = self._conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM commits").fetchone()[0]

                    # the head of the chain becomes this commit's parent, binding it to the full history behind it
                    parent_row: tuple | None = self._conn.execute(
                        "SELECT commit_hash FROM commits ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    parent_commit_hash: str | None = parent_row[0] if parent_row is not None else None
                    commit_hash: str = compute_commit_hash(
                        parent_commit_hash, seq, committed_at, added, removed, updated, user, reason
                    )

                    for added_file in added:
                        self._conn.execute(
                            "INSERT INTO objects (path, size, sha256, added_txn, added_seq) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (added_file.path, added_file.size, added_file.sha256, txn_uuid, seq),
                        )
                    for removed_file in removed:
                        self._conn.execute(
                            "UPDATE objects SET removed_txn = ?, removed_seq = ? "
                            "WHERE path = ? AND removed_seq IS NULL",
                            (txn_uuid, seq, removed_file.path),
                        )
                    for updated_file in updated:
                        # mark the old content's row as removed and insert a new live row for the replacement content,
                        # preserving the full history of the path
                        self._conn.execute(
                            "UPDATE objects SET removed_txn = ?, removed_seq = ? "
                            "WHERE path = ? AND removed_seq IS NULL",
                            (txn_uuid, seq, updated_file.path),
                        )
                        self._conn.execute(
                            "INSERT INTO objects (path, size, sha256, added_txn, added_seq) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (updated_file.path, updated_file.new_size, updated_file.new_sha256, txn_uuid, seq),
                        )
                    self._conn.execute(
                        "INSERT INTO commits "
                        "(commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, added, removed, updated, user, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            commit_hash,
                            parent_commit_hash,
                            txn_uuid,
                            seq,
                            committed_at,
                            _file_meta_list_adapter.dump_json(added).decode(),
                            _file_meta_list_adapter.dump_json(removed).decode(),
                            _updated_meta_list_adapter.dump_json(updated).decode(),
                            user,
                            reason,
                        ),
                    )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

                record = types.CommitRecord(
                    commit_hash=commit_hash,
                    parent_commit_hash=parent_commit_hash,
                    txn_uuid=txn_uuid,
                    seq=seq,
                    committed_at=committed_at,
                    added=added,
                    removed=removed,
                    updated=updated,
                    user=user,
                    reason=reason,
                )
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_list(self) -> list[types.CommitRecord]:
        with self._lock:
            rows: list[tuple] = self._conn.execute(
                "SELECT commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, added, removed, updated, user, reason "
                "FROM commits ORDER BY seq, committed_at"
            ).fetchall()
        return [self._row_to_commit_record(row) for row in rows]

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_page(self, limit: int, before_seq: int | None = None) -> types.CommitHistoryPage:
        # newest-first history, keyset-paginated by seq — one extra row beyond the limit marks truncation
        with self._lock:
            rows: list[tuple] = self._conn.execute(
                "SELECT commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, added, removed, updated, user, reason "
                "FROM commits WHERE (? IS NULL OR seq < ?) ORDER BY seq DESC LIMIT ?",
                (before_seq, before_seq, limit + 1),
            ).fetchall()

        truncated: bool = len(rows) > limit
        page_rows: list[tuple] = rows[:limit]
        return types.CommitHistoryPage(
            records=[self._row_to_commit_record(row) for row in page_rows],
            next_cursor=page_rows[-1][3] if truncated and page_rows else None,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()
