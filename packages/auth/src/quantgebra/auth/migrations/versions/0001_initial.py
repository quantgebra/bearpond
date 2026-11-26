import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "auth"


# ----------------------------------------------------------------------------------------------------------------------
def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("is_disabled", sa.Boolean(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("issuer", "subject", name="users_issuer_subject_unique"),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_users_username", "users", ["username"], schema=SCHEMA)
    op.create_index(
        "users_local_username_unique",
        "users",
        ["username"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("issuer = 'local'"),
        sqlite_where=sa.text("issuer = 'local'"),
    )

    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("auth.users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_name", sa.String(), nullable=False),
        sa.Column("provider_group_names", sa.JSON(), nullable=False),
        sa.Column("csrf_token", sa.String(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_datetime", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_sessions_user_id", "sessions", ["user_id"], schema=SCHEMA)
    op.create_index("ix_auth_sessions_expires_datetime", "sessions", ["expires_datetime"], schema=SCHEMA)

    op.create_table(
        "groups",
        sa.Column("group_name", sa.String(), primary_key=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "user_groups",
        sa.Column("user_id", sa.String(), sa.ForeignKey("auth.users.user_id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "group_name", sa.String(), sa.ForeignKey("auth.groups.group_name", ondelete="CASCADE"), primary_key=True
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_user_groups_group_name", "user_groups", ["group_name"], schema=SCHEMA)

    op.create_table(
        "provider_groups",
        sa.Column("provider_name", sa.String(), primary_key=True),
        sa.Column("provider_group_name", sa.String(), primary_key=True),
        sa.Column("seen_datetime", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "group_mappings",
        sa.Column("provider_name", sa.String(), primary_key=True),
        sa.Column("provider_group_name", sa.String(), primary_key=True),
        sa.Column(
            "group_name", sa.String(), sa.ForeignKey("auth.groups.group_name", ondelete="CASCADE"), primary_key=True
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_group_mappings_group_name", "group_mappings", ["group_name"], schema=SCHEMA)

    op.create_table(
        "auth_settings",
        sa.Column("singleton_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("session_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("is_local_enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("singleton_id = 1", name="auth_settings_singleton"),
        schema=SCHEMA,
    )

    op.create_table(
        "grant_rules",
        sa.Column("rule_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_name", sa.String(), sa.ForeignKey("auth.groups.group_name", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("permission", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("subject_kind", sa.String(), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_grant_rules_group_name", "grant_rules", ["group_name"], schema=SCHEMA)

    op.create_table(
        "local_credentials",
        sa.Column("user_id", sa.String(), sa.ForeignKey("auth.users.user_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("changed_datetime", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "invites",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("auth.users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_datetime", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_auth_invites_user_id", "invites", ["user_id"], schema=SCHEMA)

    op.create_table(
        "oidc_providers",
        sa.Column("provider_name", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("authorize_parameters", sa.JSON(), nullable=False),
        sa.Column("groups_claim", sa.String(), nullable=False),
        sa.Column("username_claim", sa.String(), nullable=False),
        sa.Column("name_claim", sa.String(), nullable=False),
        sa.Column("email_claim", sa.String(), nullable=False),
        sa.Column("logout", sa.String(16), nullable=False),
        sa.Column("is_insecure_http_allowed", sa.Boolean(), nullable=False),
        schema=SCHEMA,
    )


# ----------------------------------------------------------------------------------------------------------------------
def downgrade() -> None:
    for table_name in (
        "oidc_providers",
        "invites",
        "local_credentials",
        "grant_rules",
        "auth_settings",
        "group_mappings",
        "provider_groups",
        "user_groups",
        "groups",
        "sessions",
        "users",
    ):
        op.drop_table(table_name, schema=SCHEMA)
