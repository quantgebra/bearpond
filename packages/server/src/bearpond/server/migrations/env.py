from alembic import context

from bearpond.server import base
from bearpond.server import server_database

config = context.config

# the connection is the caller's, so that the migrations run in the caller's transaction
connection = config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=server_database.get_metadata(),
    version_table=server_database.VERSION_TABLE_NAME,
    version_table_schema=None if connection.dialect.name == "sqlite" else base.SCHEMA_NAME,
    include_schemas=False,
)
with context.begin_transaction():
    context.run_migrations()
