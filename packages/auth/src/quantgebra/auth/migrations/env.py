from alembic import context

from quantgebra.auth.core import auth_schema

config = context.config

# the schema is made by the caller with the connection it gives, so that the migrations run in the caller's transaction
connection = config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=auth_schema.get_metadata(),
    version_table=auth_schema.VERSION_TABLE_NAME,
    version_table_schema=None if connection.dialect.name == "sqlite" else auth_schema.SCHEMA_NAME,
    include_schemas=False,
)
with context.begin_transaction():
    context.run_migrations()
