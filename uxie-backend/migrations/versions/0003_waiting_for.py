"""Phase 3: background_tasks.waiting_for (wait/wake)."""
import sqlalchemy as sa
from alembic import op

revision = "0003_waiting_for"
down_revision = "0002_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("background_tasks")}
    if "waiting_for" not in cols:
        op.add_column("background_tasks", sa.Column("waiting_for", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("background_tasks", "waiting_for")
