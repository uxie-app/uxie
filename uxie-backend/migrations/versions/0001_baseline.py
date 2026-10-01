"""Baseline: the schema as it stood before Alembic (2026-09-30).

Production was built by Base.metadata.create_all + db.ADDITIVE_COLUMNS, so
this revision is an idempotent bootstrap: on an existing database it changes
nothing, on a fresh one it creates the baseline tables. It builds them from
the *live* models, so a fresh database may already have columns that later
revisions add — which is why every later revision must be existence-guarded.
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

BASELINE_TABLES = [
    "users", "otps", "usage", "referrals", "llm_usage", "stt_usage", "session_log",
    "conversations", "turns", "agent_sessions", "refresh_tokens",
    "background_tasks", "task_approvals", "task_events", "scheduled_tasks",
    "meeting_recordings", "oauth_tokens",
]


def upgrade() -> None:
    import db
    import db_ios  # noqa: F401

    bind = op.get_bind()
    tables = [db.Base.metadata.tables[name] for name in BASELINE_TABLES]
    db.Base.metadata.create_all(bind, tables=tables, checkfirst=True)
    if bind.dialect.name == "postgresql":
        for table, column, ddl in db.ADDITIVE_COLUMNS:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")


def downgrade() -> None:
    raise NotImplementedError("baseline cannot be downgraded")
