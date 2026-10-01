"""Alembic environment. Normally invoked from db.init_db(), which passes an
open connection in config.attributes["connection"]. When run from the CLI it
connects with the app's DATABASE_URL."""
import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

import db as _db
import db_ios  # noqa: F401 — registers models on Base.metadata

target_metadata = _db.Base.metadata
MIGRATION_LOCK_ID = 724501  # pg advisory lock: one replica migrates at a time


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            connection.execute(text(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})"))
        context.run_migrations()


async def _run_cli() -> None:
    engine = create_async_engine(_db._async_db_url(_db._settings.database_url), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


_conn = context.config.attributes.get("connection")
if _conn is not None:
    _run(_conn)
else:
    asyncio.run(_run_cli())
