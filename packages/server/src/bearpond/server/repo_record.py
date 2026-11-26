import sqlalchemy
from sqlalchemy import orm

from . import base


# ======================================================================================================================
class RepoRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "repos"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_by_name(session: orm.Session, name: str) -> "RepoRecord | None":
        return session.get(RepoRecord, name)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all_names(session: orm.Session) -> list[str]:
        statement: sqlalchemy.Select = sqlalchemy.select(RepoRecord.name).order_by(RepoRecord.name)
        return list(session.scalars(statement))

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_locked_by_name(session: orm.Session, name: str) -> "RepoRecord | None":
        # the repo, locked until the end of the transaction, so that whatever is decided about the repo in the
        # transaction cannot be decided differently by another one at the same time
        statement: sqlalchemy.Select = sqlalchemy.select(RepoRecord).where(RepoRecord.name == name)
        if session.get_bind().dialect.name == "sqlite":
            # SQLite has no row locks, but writing the row to what it already is takes the write lock of the whole
            # database, which a transaction that goes on to write has to have anyway; a commit that waits for it then
            # reads what the one before it wrote
            session.execute(sqlalchemy.update(RepoRecord).where(RepoRecord.name == name).values(name=name))
        else:
            statement = statement.with_for_update()
        return session.scalars(statement).first()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def register(session: orm.Session, name: str) -> None:
        # does nothing when the repo is there
        if RepoRecord.get_by_name(session, name) is None:
            session.add(RepoRecord(name=name))
            session.flush()

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
