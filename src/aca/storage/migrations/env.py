"""Migrations run only through aca db migrate with an injected, scoped connection."""

from alembic import context

from aca.storage.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use aca db migrate; direct unconfigured Alembic execution is disabled")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    version_table_schema="aca",
    compare_type=True,
    include_schemas=True,
)
with context.begin_transaction():
    context.run_migrations()
