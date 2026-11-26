from collections.abc import Callable

import sqlalchemy
from pydantic import BaseModel
from sqlalchemy import orm

from bearpond.protocol import types
from . import base
from . import repo_record


# ======================================================================================================================
class DirectoryPage(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    files: list[types.FileMetadata]
    directories: list[str]
    next_cursor: str | None


# ======================================================================================================================
class ObjectRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "objects"
    __table_args__ = (
        # at most one live row per (repo, path)
        sqlalchemy.Index(
            "objects_live_path",
            "repo",
            "path",
            unique=True,
            sqlite_where=sqlalchemy.text("removed_seq IS NULL"),
            postgresql_where=sqlalchemy.text("removed_seq IS NULL"),
        ),
        sqlalchemy.Index("objects_repo_path", "repo", "path"),
    )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_file_by_path(session: orm.Session, repo_name: str, path: str) -> types.FileMetadata | None:
        statement: sqlalchemy.Select = sqlalchemy.select(ObjectRecord).where(
            ObjectRecord.repo_name == repo_name, ObjectRecord.path == path, ObjectRecord.removed_seq.is_(None)
        )
        record: ObjectRecord | None = session.scalars(statement).first()
        result: types.FileMetadata | None = None
        if record is not None:
            result = types.FileMetadata(path=record.path, sha256=types.Sha256(record.sha256), size=record.size)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_live_files_by_path(session: orm.Session, repo_name: str) -> dict[str, types.FileMetadata]:
        # every file that is in the lake now, by path
        statement: sqlalchemy.Select = sqlalchemy.select(ObjectRecord).where(
            ObjectRecord.repo_name == repo_name, ObjectRecord.removed_seq.is_(None)
        )
        result: dict[str, types.FileMetadata] = {}
        for record in session.scalars(statement):
            result[record.path] = types.FileMetadata(
                path=record.path, sha256=types.Sha256(record.sha256), size=record.size
            )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_files_as_of_seq(session: orm.Session, repo_name: str, seq: int) -> list[types.FileMetadata]:
        # the files that were added at or before the seq and had not been removed by then, by path
        statement: sqlalchemy.Select = (
            sqlalchemy.select(ObjectRecord)
            .where(
                ObjectRecord.repo_name == repo_name,
                ObjectRecord.added_seq <= seq,
                sqlalchemy.or_(ObjectRecord.removed_seq.is_(None), ObjectRecord.removed_seq > seq),
            )
            .order_by(ObjectRecord.path)
        )
        result: list[types.FileMetadata] = []
        for record in session.scalars(statement):
            result.append(types.FileMetadata(path=record.path, sha256=types.Sha256(record.sha256), size=record.size))
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_directory_page(
        session: orm.Session, repo_name: str, prefix: str, limit: int, cursor: str | None = None
    ) -> DirectoryPage:
        # S3-style prefix+delimiter ("/") listing over the live mapping: files directly under the prefix, and the
        # distinct immediate subdirectories rolled up in the database (the standard's Contents + CommonPrefixes); files
        # paginate by key (the cursor is the last file's full path from a previous page); directories are returned in
        # full, since they are few by construction
        position: Callable[..., sqlalchemy.ColumnElement] = ObjectRecord._get_position_function(session)
        remainder: sqlalchemy.ColumnElement = sqlalchemy.func.substr(ObjectRecord.path, len(prefix) + 1)
        is_live_under_prefix: list[sqlalchemy.ColumnElement] = [
            ObjectRecord.repo_name == repo_name,
            ObjectRecord.removed_seq.is_(None),
            sqlalchemy.func.substr(ObjectRecord.path, 1, len(prefix)) == prefix,
        ]

        # one row beyond the limit is how we know the page is truncated
        file_statement: sqlalchemy.Select = (
            sqlalchemy.select(ObjectRecord)
            .where(*is_live_under_prefix, position(remainder, "/") == 0, ObjectRecord.path > (cursor or ""))
            .order_by(ObjectRecord.path)
            .limit(limit + 1)
        )
        file_records: list[ObjectRecord] = list(session.scalars(file_statement))
        directory_name: sqlalchemy.ColumnElement = sqlalchemy.func.substr(
            ObjectRecord.path, 1, len(prefix) + position(remainder, "/") - 1
        )
        directory_statement: sqlalchemy.Select = (
            sqlalchemy.select(directory_name)
            .where(*is_live_under_prefix, position(remainder, "/") > 0)
            .distinct()
            .order_by(directory_name)
        )
        directory_names: list[str] = list(session.scalars(directory_statement))

        is_truncated: bool = len(file_records) > limit
        page_records: list[ObjectRecord] = file_records[:limit]
        files: list[types.FileMetadata] = []
        for record in page_records:
            files.append(types.FileMetadata(path=record.path, sha256=types.Sha256(record.sha256), size=record.size))
        return DirectoryPage(
            files=files,
            directories=directory_names,
            next_cursor=page_records[-1].path if is_truncated and page_records else None,
        )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def add_live_file(session: orm.Session, repo_name: str, file: types.FileMetadata, txn_uuid: str, seq: int) -> None:
        session.add(
            ObjectRecord(
                repo_name=repo_name,
                path=file.path,
                sha256=file.sha256,
                size=file.size,
                added_txn=txn_uuid,
                added_seq=seq,
            )
        )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def remove_live_file(session: orm.Session, repo_name: str, path: str, txn_uuid: str, seq: int) -> None:
        # marks the live row of the path as removed by the commit, which keeps the whole history of the path
        session.execute(
            sqlalchemy.update(ObjectRecord)
            .where(ObjectRecord.repo_name == repo_name, ObjectRecord.path == path, ObjectRecord.removed_seq.is_(None))
            .values(removed_txn=txn_uuid, removed_seq=seq)
        )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _get_position_function(session: orm.Session) -> Callable[..., sqlalchemy.ColumnElement]:
        # where a text is first found in another, 0 for nowhere, which the databases call different things
        position: Callable[..., sqlalchemy.ColumnElement] = sqlalchemy.func.instr
        if session.get_bind().dialect.name == "postgresql":
            position = sqlalchemy.func.strpos
        return position

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    id: orm.Mapped[int] = orm.mapped_column(primary_key=True, autoincrement=True)
    repo_name: orm.Mapped[str] = orm.mapped_column("repo", sqlalchemy.ForeignKey(repo_record.RepoRecord.name))
    path: orm.Mapped[str]
    sha256: orm.Mapped[str]
    size: orm.Mapped[int]
    added_txn: orm.Mapped[str]
    added_seq: orm.Mapped[int]
    removed_txn: orm.Mapped[str | None]
    removed_seq: orm.Mapped[int | None]
