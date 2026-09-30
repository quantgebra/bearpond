import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter

from .. import types
from . import metadata_store
from . import transaction

# validates/serializes the added/removed FileMetadata lists stored as JSON text in the commits and transactions tables
_file_meta_list_adapter: TypeAdapter[list[types.FileMetadata]] = TypeAdapter(list[types.FileMetadata])

# validates/serializes the updated UpdatedFileMetadata list stored as JSON text in the commits and transactions tables
_updated_meta_list_adapter: TypeAdapter[list[types.UpdatedFileMetadata]] = TypeAdapter(list[types.UpdatedFileMetadata])


# ======================================================================================================================
class SqliteMetadataStore(metadata_store.MetadataStore):

    # one database per server. each table carries a repo column so multiple repositories can share the same file.
    # seq remains per-repo, and txn_uuid is globally unique.
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS repos (
        name TEXT PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS objects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        added_txn TEXT NOT NULL,
        added_seq INTEGER NOT NULL,
        removed_txn TEXT,
        removed_seq INTEGER,
        FOREIGN KEY (repo) REFERENCES repos(name)
    );
    -- at most one live row per (repo, path)
    CREATE UNIQUE INDEX IF NOT EXISTS objects_live_path ON objects(repo, path) WHERE removed_seq IS NULL;
    CREATE INDEX IF NOT EXISTS objects_repo_path ON objects(repo, path);

    CREATE TABLE IF NOT EXISTS commits (
        repo TEXT NOT NULL,
        commit_hash TEXT NOT NULL,
        parent_commit_hash TEXT,
        txn_uuid TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        committed_at TEXT NOT NULL,
        created_at TEXT,
        added TEXT NOT NULL,
        removed TEXT NOT NULL,
        updated TEXT NOT NULL,
        user TEXT,
        reason TEXT,
        FOREIGN KEY (repo) REFERENCES repos(name)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS commits_repo_seq ON commits(repo, seq);
    CREATE INDEX IF NOT EXISTS commits_repo_txn ON commits(repo, txn_uuid);

    CREATE TABLE IF NOT EXISTS transactions (
        repo TEXT NOT NULL,
        txn_uuid TEXT NOT NULL,
        created_at TEXT NOT NULL,
        added TEXT NOT NULL,
        removed TEXT NOT NULL,
        updated TEXT NOT NULL,
        user TEXT,
        reason TEXT,
        PRIMARY KEY (repo, txn_uuid),
        FOREIGN KEY (repo) REFERENCES repos(name)
    );
    CREATE INDEX IF NOT EXISTS transactions_repo_txn ON transactions(repo, txn_uuid);

    -- one row per file uploaded against an in-flight transaction; inserting a row per upload beats rewriting
    -- an ever-growing JSON blob on the transaction record
    CREATE TABLE IF NOT EXISTS transaction_uploaded_files (
        repo TEXT NOT NULL,
        txn_uuid TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY (repo, txn_uuid, path),
        FOREIGN KEY (repo, txn_uuid) REFERENCES transactions(repo, txn_uuid)
    );
    """

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _row_to_commit_record(row: tuple) -> types.CommitRecord:
        # row columns arrive in the same order the model declares them: commit_hash, parent_commit_hash, txn_uuid,
        # seq, committed_at, created_at, added, removed, updated, user, reason
        return types.CommitRecord(
            commit_hash=row[0],
            parent_commit_hash=row[1],
            txn_uuid=row[2],
            seq=row[3],
            committed_at=row[4],
            created_at=row[5],
            added=_file_meta_list_adapter.validate_json(row[6]),
            removed=_file_meta_list_adapter.validate_json(row[7]),
            updated=_updated_meta_list_adapter.validate_json(row[8]),
            user=row[9],
            reason=row[10],
        )

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # create the connection. check_same_thread=False bc we will have different threads.
        self._conn: sqlite3.Connection = sqlite3.connect(db_path, check_same_thread=False)

        # re-entrant, so methods can call each other (commit_transaction → get_commit_record) without self-deadlock
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
    def get_file_metadata(self, repo: str, path: str) -> types.FileMetadata | None:
        with self._lock:
            row: tuple | None = self._conn.execute(
                "SELECT sha256, size FROM objects WHERE repo = ? AND path = ? AND removed_seq IS NULL", (repo, path)
            ).fetchone()
        result: types.FileMetadata | None = None
        if row is not None:
            result = types.FileMetadata(path=path, sha256=row[0], size=row[1])
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def list_directory(self, repo: str, prefix: str, limit: int, cursor: str | None = None) -> "metadata_store.DirectoryPage":
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
                "SELECT path, sha256, size FROM objects "
                "WHERE repo = ? AND removed_seq IS NULL AND path GLOB ? AND instr(substr(path, ?), '/') = 0 AND path > ? "
                "ORDER BY path LIMIT ?",
                (repo, glob_prefix, rest_start, cursor or "", limit + 1),
            ).fetchall()
            dir_rows: list[tuple] = self._conn.execute(
                "SELECT DISTINCT substr(path, 1, ? + instr(substr(path, ?), '/') - 1) AS dir FROM objects "
                "WHERE repo = ? AND removed_seq IS NULL AND path GLOB ? AND instr(substr(path, ?), '/') > 0 ORDER BY dir",
                (len(prefix), rest_start, repo, glob_prefix, rest_start),
            ).fetchall()

        truncated: bool = len(file_rows) > limit
        page_rows: list[tuple] = file_rows[:limit]
        files: list[types.FileMetadata] = []
        for row in page_rows:
            file_metadata: types.FileMetadata = types.FileMetadata(path=row[0], sha256=row[1], size=row[2])
            files.append(file_metadata)
        return metadata_store.DirectoryPage(
            files=files,
            directories=[row[0] for row in dir_rows],
            next_cursor=page_rows[-1][0] if truncated and page_rows else None,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def get_current_seq(self, repo: str) -> int:
        with self._lock:
            # COALESCE covers the empty commits table: an aggregate query always returns one row, MAX of zero rows
            # is NULL, and 0 is the lake's "no commits yet" sentinel — the first commit gets seq 1
            seq: int = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM commits WHERE repo = ?", (repo,)
            ).fetchone()[0]
        return seq

    # ------------------------------------------------------------------------------------------------------------------
    def get_manifest(self, repo: str, seq: int) -> types.Manifest | None:
        # the lake as of the given seq: everything added at or before it and not yet removed by then. Returns
        # None for a seq with no commit record (0 — the never-committed state — or beyond the current seq).
        with self._lock:
            # every recorded commit advances the seq exactly once, so each seq has exactly one commit record
            row: tuple | None = self._conn.execute(
                "SELECT committed_at FROM commits WHERE repo = ? AND seq = ?", (repo, seq)
            ).fetchone()

            # if there is a commit with the given seq
            manifest: types.Manifest | None = None
            if row is not None:
                # select all the files that were added before or by seq and have been deleted (yet)
                rows: list[tuple] = self._conn.execute(
                    "SELECT path, sha256, size FROM objects "
                    "WHERE repo = ? AND added_seq <= ? AND (removed_seq IS NULL OR removed_seq > ?) ORDER BY path",
                    (repo, seq, seq),
                ).fetchall()

                # creat the Manifest with the selected FileMetadata
                files: list[types.FileMetadata] = []
                for r in rows:
                    file_metadata: types.FileMetadata = types.FileMetadata(path=r[0], sha256=r[1], size=r[2])
                    files.append(file_metadata)
                manifest = types.Manifest(seq=seq, created_at=row[0], files=files)
        return manifest

    # ------------------------------------------------------------------------------------------------------------------
    def _get_commit_record_by(self, repo: str, where: str, value: object) -> types.CommitRecord | None:
        with self._lock:
            row: tuple | None = self._conn.execute(
                "SELECT commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, created_at, added, removed, updated, user, reason "
                f"FROM commits WHERE repo = ? AND {where} = ?",
                (repo, value),
            ).fetchone()
        record: types.CommitRecord | None = None
        if row is not None:
            record = self._row_to_commit_record(row)
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_by_txn_uuid(self, repo: str, txn_uuid: types.TxnUuid) -> types.CommitRecord | None:
        return self._get_commit_record_by(repo, "txn_uuid", txn_uuid)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_by_commit_hash(self, repo: str, commit_hash: str) -> types.CommitRecord | None:
        return self._get_commit_record_by(repo, "commit_hash", commit_hash)

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_by_commit_seq(self, repo: str, seq: int) -> types.CommitRecord | None:
        return self._get_commit_record_by(repo, "seq", seq)

    # ------------------------------------------------------------------------------------------------------------------
    def commit_transaction(self, repo: str, txn: transaction.Transaction) -> types.CommitRecord:
        record: types.CommitRecord
        with self._lock:
            # a commit that already recorded itself is a retry after a lost response — answer with the original record
            existing: types.CommitRecord | None = self.get_commit_record_by_txn_uuid(repo, txn.txn_uuid)
            if existing is not None:
                record = existing
            else:
                committed_at: str = datetime.now(timezone.utc).isoformat()

                # BEGIN IMMEDIATE takes the write lock up front, so the checks below and the updates they guard are
                # one atomic unit — a concurrent commit can't slip between check and act
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    # ensure the repo is registered before committing — idempotent insert
                    self._conn.execute("INSERT OR IGNORE INTO repos (name) VALUES (?)", (repo,))

                    # create map of current repo contents
                    rows: list[tuple] = self._conn.execute(
                        "SELECT path, sha256, size FROM objects WHERE repo = ? AND removed_seq IS NULL", (repo,)
                    ).fetchall()
                    current_path_2_metadata: dict[str, types.FileMetadata] = {}
                    for row in rows:
                        file_metadata: types.FileMetadata = types.FileMetadata(path=row[0], sha256=row[1], size=row[2])
                        current_path_2_metadata[file_metadata.path] = file_metadata

                    # a path can only be in one of added, removed, or updated within a single commit
                    added_paths: set[str] = {f.path for f in txn.added}
                    removed_paths: set[str] = {f.path for f in txn.removed}
                    updated_paths: set[str] = {f.path for f in txn.updated}
                    for path in added_paths & removed_paths:
                        raise metadata_store.MetadataConflictError(f"path cannot be both added and removed: {path}")
                    for path in added_paths & updated_paths:
                        raise metadata_store.MetadataConflictError(f"path cannot be both added and updated: {path}")
                    for path in removed_paths & updated_paths:
                        raise metadata_store.MetadataConflictError(f"path cannot be both removed and updated: {path}")

                    # an added path must not be live at all — different content requires a remove first, identical
                    # content is a no-op, and both are rejected: every recorded commit changes the lake by construction
                    for added_file in txn.added:
                        if added_file.path in current_path_2_metadata:
                            if current_path_2_metadata[added_file.path].sha256 != added_file.sha256:
                                raise metadata_store.MetadataConflictError(
                                    f"{added_file.path} already exists in the lake with different content — "
                                    f"remove it before adding new content"
                                )
                            raise metadata_store.MetadataConflictError(
                                f"{added_file.path} already exists in the lake with identical content — "
                                f"re-adding it is a no-op"
                            )

                    # a removal is only safe against the exact content the client observed when it decided to remove
                    for removed_file in txn.removed:
                        if removed_file.path not in current_path_2_metadata:
                            raise metadata_store.MetadataConflictError(f"path to remove no longer exists: {removed_file.path}")
                        if current_path_2_metadata[removed_file.path].sha256 != removed_file.sha256:
                            raise metadata_store.MetadataConflictError(
                                f"{removed_file.path} has changed since it was declared for removal — "
                                f"remove is only safe against the exact content that was observed"
                            )

                    # an update is only safe against the exact content the client observed, and the new content must
                    # actually be different — otherwise the commit would be a no-op
                    for updated_file in txn.updated:
                        if updated_file.path not in current_path_2_metadata:
                            raise metadata_store.MetadataConflictError(f"path to update no longer exists: {updated_file.path}")
                        if current_path_2_metadata[updated_file.path].sha256 != updated_file.old_sha256:
                            raise metadata_store.MetadataConflictError(
                                f"{updated_file.path} has changed since it was declared for update — "
                                f"update is only safe against the exact content that was observed"
                            )
                        if updated_file.old_sha256 == updated_file.new_sha256:
                            raise metadata_store.MetadataConflictError(
                                f"{updated_file.path} old and new content are identical — updating it is a no-op"
                            )

                    # the checks above guarantee this commit changes the mapping, so the seq always advances
                    seq: int = self._conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM commits WHERE repo = ?", (repo,)
                    ).fetchone()[0]

                    # the head of the chain becomes this commit's parent, binding it to the full history behind it
                    parent_row: tuple | None = self._conn.execute(
                        "SELECT commit_hash FROM commits WHERE repo = ? ORDER BY seq DESC LIMIT 1", (repo,)
                    ).fetchone()
                    parent_commit_hash: str | None = parent_row[0] if parent_row is not None else None
                    commit_hash: str = metadata_store.compute_commit_hash(
                        parent_commit_hash, seq, committed_at, txn.added, txn.removed, txn.updated, txn.user, txn.reason
                    )

                    for added_file in txn.added:
                        self._conn.execute(
                            "INSERT INTO objects (repo, path, sha256, size, added_txn, added_seq) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (repo, added_file.path, added_file.sha256, added_file.size, txn.txn_uuid, seq),
                        )
                    for removed_file in txn.removed:
                        self._conn.execute(
                            "UPDATE objects SET removed_txn = ?, removed_seq = ? "
                            "WHERE repo = ? AND path = ? AND removed_seq IS NULL",
                            (txn.txn_uuid, seq, repo, removed_file.path),
                        )
                    for updated_file in txn.updated:
                        # mark the old content's row as removed and insert a new live row for the replacement content,
                        # preserving the full history of the path
                        self._conn.execute(
                            "UPDATE objects SET removed_txn = ?, removed_seq = ? "
                            "WHERE repo = ? AND path = ? AND removed_seq IS NULL",
                            (txn.txn_uuid, seq, repo, updated_file.path),
                        )
                        self._conn.execute(
                            "INSERT INTO objects (repo, path, sha256, size, added_txn, added_seq) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (repo, updated_file.path, updated_file.new_sha256, updated_file.new_size, txn.txn_uuid, seq),
                        )
                    self._conn.execute(
                        "INSERT INTO commits "
                        "(repo, commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, created_at, added, removed, updated, user, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            repo,
                            commit_hash,
                            parent_commit_hash,
                            txn.txn_uuid,
                            seq,
                            committed_at,
                            txn.created_at,
                            _file_meta_list_adapter.dump_json(txn.added).decode(),
                            _file_meta_list_adapter.dump_json(txn.removed).decode(),
                            _updated_meta_list_adapter.dump_json(txn.updated).decode(),
                            txn.user,
                            txn.reason,
                        ),
                    )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

                record = types.CommitRecord(
                    commit_hash=commit_hash,
                    parent_commit_hash=parent_commit_hash,
                    txn_uuid=txn.txn_uuid,
                    seq=seq,
                    committed_at=committed_at,
                    created_at=txn.created_at,
                    added=txn.added,
                    removed=txn.removed,
                    updated=txn.updated,
                    user=txn.user,
                    reason=txn.reason,
                )
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_list(self, repo: str) -> list[types.CommitRecord]:
        with self._lock:
            rows: list[tuple] = self._conn.execute(
                "SELECT commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, created_at, added, removed, updated, user, reason "
                "FROM commits WHERE repo = ? ORDER BY seq, committed_at",
                (repo,),
            ).fetchall()
        records: list[types.CommitRecord] = []
        for row in rows:
            records.append(self._row_to_commit_record(row))
        return records

    # ------------------------------------------------------------------------------------------------------------------
    def get_commit_record_page(self, repo: str, limit: int, before_seq: int | None = None) -> types.CommitHistoryPage:
        # newest-first history, keyset-paginated by seq — one extra row beyond the limit marks truncation
        with self._lock:
            rows: list[tuple] = self._conn.execute(
                "SELECT commit_hash, parent_commit_hash, txn_uuid, seq, committed_at, created_at, added, removed, updated, user, reason "
                "FROM commits WHERE repo = ? AND (? IS NULL OR seq < ?) ORDER BY seq DESC LIMIT ?",
                (repo, before_seq, before_seq, limit + 1),
            ).fetchall()

        truncated: bool = len(rows) > limit
        page_rows: list[tuple] = rows[:limit]
        records: list[types.CommitRecord] = []
        for row in page_rows:
            records.append(self._row_to_commit_record(row))
        return types.CommitHistoryPage(
            records=records,
            next_cursor=page_rows[-1][3] if truncated and page_rows else None,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def register_repo(self, repo: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO repos (name) VALUES (?)", (repo,))
            self._conn.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def list_repos(self) -> list[str]:
        with self._lock:
            rows: list[tuple] = self._conn.execute("SELECT name FROM repos ORDER BY name").fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------------------------------------------------------
    def store_transaction(self, repo: str, txn: transaction.Transaction) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO transactions (repo, txn_uuid, created_at, added, removed, updated, user, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    txn.txn_uuid,
                    txn.created_at,
                    _file_meta_list_adapter.dump_json(txn.added).decode(),
                    _file_meta_list_adapter.dump_json(txn.removed).decode(),
                    _updated_meta_list_adapter.dump_json(txn.updated).decode(),
                    txn.user,
                    txn.reason,
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, repo: str, txn_uuid: types.TxnUuid) -> transaction.Transaction | None:
        with self._lock:
            row: tuple | None = self._conn.execute(
                "SELECT created_at, added, removed, updated, user, reason FROM transactions WHERE repo = ? AND txn_uuid = ?",
                (repo, txn_uuid),
            ).fetchone()
            result: transaction.Transaction | None
            if row is None:
                result = None
            else:
                uploaded_rows: list[tuple] = self._conn.execute(
                    "SELECT path, sha256, size FROM transaction_uploaded_files WHERE repo = ? AND txn_uuid = ? ORDER BY path",
                    (repo, txn_uuid),
                ).fetchall()
                result = transaction.Transaction(
                    txn_uuid=txn_uuid,
                    created_at=row[0],
                    added=_file_meta_list_adapter.validate_json(row[1]),
                    removed=_file_meta_list_adapter.validate_json(row[2]),
                    updated=_updated_meta_list_adapter.validate_json(row[3]),
                    uploaded=[
                        types.FileMetadata(path=path, sha256=sha256, size=size)
                        for path, sha256, size in uploaded_rows
                    ],
                    user=row[4],
                    reason=row[5],
                )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def record_uploaded_file(self, repo: str, txn_uuid: types.TxnUuid, file: types.FileMetadata) -> None:
        # INSERT OR REPLACE so a retried upload of the same path is idempotent — the declared hash is fixed at
        # begin time, so a retry can only ever rewrite the same row
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO transaction_uploaded_files (repo, txn_uuid, path, sha256, size) VALUES (?, ?, ?, ?, ?)",
                (repo, txn_uuid, file.path, file.sha256, file.size),
            )
            self._conn.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def delete_transaction(self, repo: str, txn_uuid: types.TxnUuid) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM transaction_uploaded_files WHERE repo = ? AND txn_uuid = ?", (repo, txn_uuid)
            )
            self._conn.execute("DELETE FROM transactions WHERE repo = ? AND txn_uuid = ?", (repo, txn_uuid))
            self._conn.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()
