from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import url as sa_url

# P1-3: backend-aware migration target. The canonical records table is
# defined by the concrete backends; SQLite and PostgreSQL schemas must
# converge through the same versioned lifecycle. env.py selects the
# target metadata from the configured URL so one alembic tree serves
# both engines.

config = context.config

_configured_url = config.get_main_option("sqlalchemy.url", "")
_driver = sa_url.make_url(_configured_url).get_backend_name()

if _driver.startswith("postgres"):
    from severian.infrastructure.pgvector_index import metadata as pgvector_metadata
    from severian.infrastructure.postgresql import metadata as pg_metadata

    target_metadata = pg_metadata
    # Merge the pgvector projection table into the migration target so
    # the vector index is versioned alongside the canonical store.
    for table in pgvector_metadata.tables.values():
        target_metadata._add_table(table.name, table.schema, table)
else:
    from severian.infrastructure.sqlite import metadata as sqlite_metadata

    target_metadata = sqlite_metadata

if context.is_offline_mode():
    context.configure(url=_configured_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = create_engine(_configured_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
