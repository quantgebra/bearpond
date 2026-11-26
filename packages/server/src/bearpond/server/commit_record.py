import hashlib
import json

import sqlalchemy
from pydantic import TypeAdapter
from sqlalchemy import orm

from bearpond.protocol import types
from . import base
from . import repo_record

# turns the added, removed and updated lists that are stored as JSON back into the wire types
_FILE_LIST_ADAPTER: TypeAdapter[list[types.FileMetadata]] = TypeAdapter(list[types.FileMetadata])
_UPDATED_LIST_ADAPTER: TypeAdapter[list[types.UpdatedFileMetadata]] = TypeAdapter(list[types.UpdatedFileMetadata])


# ======================================================================================================================
class CommitRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "commits"
    __table_args__ = (
        sqlalchemy.Index("commits_repo_seq", "repo", "seq", unique=True),
        sqlalchemy.Index("commits_repo_txn", "repo", "txn_uuid"),
    )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
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
        # every commit has to hash the same way whatever database it is in, so that hashes are comparable; the fixed
        # field order and compact separators make the hash reproducible from the exported record alone, and the lists
        # are sorted by path before they are written, since they are semantically sets and the hash must depend only on
        # the change's content, never on list order (the same reason git stores tree entries sorted by name)
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

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_commit_by_txn_uuid(session: orm.Session, repo_name: str, txn_uuid: str) -> types.CommitRecord | None:
        return CommitRecord._get_commit_where(session, repo_name, CommitRecord.txn_uuid == txn_uuid)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_commit_by_commit_hash(session: orm.Session, repo_name: str, commit_hash: str) -> types.CommitRecord | None:
        return CommitRecord._get_commit_where(session, repo_name, CommitRecord.commit_hash == commit_hash)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_commit_by_seq(session: orm.Session, repo_name: str, seq: int) -> types.CommitRecord | None:
        return CommitRecord._get_commit_where(session, repo_name, CommitRecord.seq == seq)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all_commits(session: orm.Session, repo_name: str) -> list[types.CommitRecord]:
        statement: sqlalchemy.Select = (
            sqlalchemy.select(CommitRecord)
            .where(CommitRecord.repo_name == repo_name)
            .order_by(CommitRecord.seq, CommitRecord.committed_at)
        )
        result: list[types.CommitRecord] = []
        for record in session.scalars(statement):
            result.append(record.to_wire())
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_commit_page(
        session: orm.Session, repo_name: str, limit: int, before_seq: int | None = None
    ) -> types.CommitHistoryPage:
        # newest-first history, keyset-paginated by seq; one row beyond the limit is how we know it is truncated
        statement: sqlalchemy.Select = (
            sqlalchemy.select(CommitRecord).where(CommitRecord.repo_name == repo_name).order_by(CommitRecord.seq.desc())
        )
        if before_seq is not None:
            statement = statement.where(CommitRecord.seq < before_seq)
        records: list[CommitRecord] = list(session.scalars(statement.limit(limit + 1)))

        is_truncated: bool = len(records) > limit
        page_records: list[CommitRecord] = records[:limit]
        commits: list[types.CommitRecord] = []
        for record in page_records:
            commits.append(record.to_wire())
        return types.CommitHistoryPage(
            records=commits, next_cursor=page_records[-1].seq if is_truncated and page_records else None
        )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_current_seq(session: orm.Session, repo_name: str) -> int:
        # 0 is the lake's "no commits yet" sentinel, so the first commit gets seq 1
        statement: sqlalchemy.Select = sqlalchemy.select(sqlalchemy.func.max(CommitRecord.seq)).where(
            CommitRecord.repo_name == repo_name
        )
        return int(session.scalar(statement) or 0)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_committed_at_by_seq(session: orm.Session, repo_name: str, seq: int) -> str | None:
        statement: sqlalchemy.Select = sqlalchemy.select(CommitRecord.committed_at).where(
            CommitRecord.repo_name == repo_name, CommitRecord.seq == seq
        )
        return session.scalars(statement).first()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_head_commit_hash(session: orm.Session, repo_name: str) -> str | None:
        # the head of the chain, which the next commit takes as its parent, binding it to the history behind it
        statement: sqlalchemy.Select = (
            sqlalchemy.select(CommitRecord.commit_hash)
            .where(CommitRecord.repo_name == repo_name)
            .order_by(CommitRecord.seq.desc())
            .limit(1)
        )
        return session.scalars(statement).first()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def add_commit(session: orm.Session, repo_name: str, commit: types.CommitRecord) -> None:
        session.add(
            CommitRecord(
                repo_name=repo_name,
                commit_hash=commit.commit_hash,
                parent_commit_hash=commit.parent_commit_hash,
                txn_uuid=commit.txn_uuid,
                seq=commit.seq,
                committed_at=commit.committed_at,
                created_at=commit.created_at,
                added=[f.model_dump() for f in commit.added],
                removed=[f.model_dump() for f in commit.removed],
                updated=[f.model_dump() for f in commit.updated],
                user=commit.user,
                reason=commit.reason,
            )
        )
        session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _get_commit_where(
        session: orm.Session, repo_name: str, condition: sqlalchemy.ColumnElement
    ) -> types.CommitRecord | None:
        statement: sqlalchemy.Select = sqlalchemy.select(CommitRecord).where(
            CommitRecord.repo_name == repo_name, condition
        )
        record: CommitRecord | None = session.scalars(statement).first()
        result: types.CommitRecord | None = None
        if record is not None:
            result = record.to_wire()
        return result

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    repo_name: orm.Mapped[str] = orm.mapped_column("repo", sqlalchemy.ForeignKey(repo_record.RepoRecord.name))
    commit_hash: orm.Mapped[str]
    parent_commit_hash: orm.Mapped[str | None]
    txn_uuid: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    seq: orm.Mapped[int]
    committed_at: orm.Mapped[str]
    created_at: orm.Mapped[str | None]
    added: orm.Mapped[list[dict[str, object]]] = orm.mapped_column(sqlalchemy.JSON)
    removed: orm.Mapped[list[dict[str, object]]] = orm.mapped_column(sqlalchemy.JSON)
    updated: orm.Mapped[list[dict[str, object]]] = orm.mapped_column(sqlalchemy.JSON)
    user: orm.Mapped[str | None]
    reason: orm.Mapped[str | None]

    # ------------------------------------------------------------------------------------------------------------------
    def to_wire(self) -> types.CommitRecord:
        result: types.CommitRecord = types.CommitRecord(
            commit_hash=self.commit_hash,
            parent_commit_hash=self.parent_commit_hash,
            txn_uuid=types.TxnUuid(self.txn_uuid),
            seq=self.seq,
            committed_at=self.committed_at,
            created_at=self.created_at,
            added=_FILE_LIST_ADAPTER.validate_python(self.added),
            removed=_FILE_LIST_ADAPTER.validate_python(self.removed),
            updated=_UPDATED_LIST_ADAPTER.validate_python(self.updated),
            user=self.user,
            reason=self.reason,
        )
        return result
