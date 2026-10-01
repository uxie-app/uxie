"""Alembic: fresh database + idempotent re-run + pre-Alembic database."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import db


async def _tables_and_version(engine):
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        version = (await conn.execute(sa.text("SELECT version_num FROM alembic_version"))).scalar_one()
        cols = await conn.run_sync(lambda c: {x["name"] for x in sa.inspect(c).get_columns("background_tasks")})
    return tables, version, cols


async def test_fresh_database_upgrades_to_head_twice(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
    for _ in range(2):  # second run must be a no-op
        async with engine.begin() as conn:
            await conn.run_sync(db.run_migrations)
    tables, version, cols = await _tables_and_version(engine)
    assert {"users", "background_tasks", "task_approvals", "agents"} <= tables
    assert version == "0004_desktop_context"
    assert "agent_id" in cols
    await engine.dispose()


async def test_pre_alembic_database_gets_new_column(tmp_path):
    # Simulate prod: tables exist (old shape, no agent_id), no alembic_version.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: c.execute(sa.text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, tier VARCHAR NOT NULL, "
            "referral_code VARCHAR NOT NULL, free_days_remaining INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        )))
        await conn.run_sync(lambda c: c.execute(sa.text(
            "CREATE TABLE background_tasks (id VARCHAR PRIMARY KEY, user_id INTEGER NOT NULL, prompt TEXT NOT NULL, "
            "status VARCHAR NOT NULL, result_md TEXT, error TEXT, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, completed_at DATETIME)"
        )))
    async with engine.begin() as conn:
        await conn.run_sync(db.run_migrations)
    tables, version, cols = await _tables_and_version(engine)
    assert "agents" in tables and version == "0004_desktop_context"
    assert "agent_id" in cols
    await engine.dispose()
