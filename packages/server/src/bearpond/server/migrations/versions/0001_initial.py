import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "bearpond"


# ----------------------------------------------------------------------------------------------------------------------
def _has_table(table_name: str) -> bool:
    # a database that an earlier version of the server made, with code of its own, already has the tables; SQLite has
    # no schemas to look in
    connection = op.get_bind()
    return sa.inspect(connection).has_table(table_name, schema=None if connection.dialect.name == "sqlite" else SCHEMA)


# ----------------------------------------------------------------------------------------------------------------------
def _has_column(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    schema = None if connection.dialect.name == "sqlite" else SCHEMA
    columns = sa.inspect(connection).get_columns(table_name, schema=schema)
    return any(column["name"] == column_name for column in columns)


# ----------------------------------------------------------------------------------------------------------------------
def upgrade() -> None:
    if not _has_table("repos"):
        op.create_table("repos", sa.Column("name", sa.String(), primary_key=True), schema=SCHEMA)

    if not _has_table("objects"):
        op.create_table(
            "objects",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("repo", sa.String(), sa.ForeignKey("bearpond.repos.name"), nullable=False),
            sa.Column("path", sa.String(), nullable=False),
            sa.Column("sha256", sa.String(), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("added_txn", sa.String(), nullable=False),
            sa.Column("added_seq", sa.Integer(), nullable=False),
            sa.Column("removed_txn", sa.String(), nullable=True),
            sa.Column("removed_seq", sa.Integer(), nullable=True),
            schema=SCHEMA,
        )
        op.create_index(
            "objects_live_path",
            "objects",
            ["repo", "path"],
            unique=True,
            schema=SCHEMA,
            postgresql_where=sa.text("removed_seq IS NULL"),
            sqlite_where=sa.text("removed_seq IS NULL"),
        )
        op.create_index("objects_repo_path", "objects", ["repo", "path"], schema=SCHEMA)

    if not _has_table("commits"):
        op.create_table(
            "commits",
            sa.Column("repo", sa.String(), sa.ForeignKey("bearpond.repos.name"), nullable=False),
            sa.Column("commit_hash", sa.String(), nullable=False),
            sa.Column("parent_commit_hash", sa.String(), nullable=True),
            sa.Column("txn_uuid", sa.String(), primary_key=True),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("committed_at", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=True),
            sa.Column("added", sa.JSON(), nullable=False),
            sa.Column("removed", sa.JSON(), nullable=False),
            sa.Column("updated", sa.JSON(), nullable=False),
            sa.Column("user", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            schema=SCHEMA,
        )
        op.create_index("commits_repo_seq", "commits", ["repo", "seq"], unique=True, schema=SCHEMA)
        op.create_index("commits_repo_txn", "commits", ["repo", "txn_uuid"], schema=SCHEMA)
    elif not _has_column("commits", "updated"):
        # the oldest databases were made before commits could update a file
        op.add_column("commits", sa.Column("updated", sa.JSON(), nullable=False, server_default="[]"), schema=SCHEMA)

    if not _has_table("transactions"):
        op.create_table(
            "transactions",
            sa.Column("repo", sa.String(), sa.ForeignKey("bearpond.repos.name"), primary_key=True),
            sa.Column("txn_uuid", sa.String(), primary_key=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("added", sa.JSON(), nullable=False),
            sa.Column("removed", sa.JSON(), nullable=False),
            sa.Column("updated", sa.JSON(), nullable=False),
            sa.Column("user", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            schema=SCHEMA,
        )
        op.create_index("transactions_repo_txn", "transactions", ["repo", "txn_uuid"], schema=SCHEMA)

    if not _has_table("transaction_uploaded_files"):
        op.create_table(
            "transaction_uploaded_files",
            sa.Column("repo", sa.String(), primary_key=True),
            sa.Column("txn_uuid", sa.String(), primary_key=True),
            sa.Column("path", sa.String(), primary_key=True),
            sa.Column("sha256", sa.String(), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["repo", "txn_uuid"], ["bearpond.transactions.repo", "bearpond.transactions.txn_uuid"]
            ),
            schema=SCHEMA,
        )


# ----------------------------------------------------------------------------------------------------------------------
def downgrade() -> None:
    for table_name in ("transaction_uploaded_files", "transactions", "commits", "objects", "repos"):
        op.drop_table(table_name, schema=SCHEMA)
