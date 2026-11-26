import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "auth"


# ----------------------------------------------------------------------------------------------------------------------
def upgrade() -> None:
    # the api keys that scripts and CI send in place of logging in; only a hash of each secret is kept
    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(), primary_key=True),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("key_name", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("auth.users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_datetime", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_api_keys_user_id", "api_keys", ["user_id"], schema=SCHEMA)


# ----------------------------------------------------------------------------------------------------------------------
def downgrade() -> None:
    op.drop_table("api_keys", schema=SCHEMA)
