"""Phase 2: persistent agents + background_tasks.agent_id."""
import sqlalchemy as sa
from alembic import op

revision = "0002_agents"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _inspector()
    if not insp.has_table("agents"):
        op.create_table(
            "agents",
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("role", sa.Text, nullable=True),
            sa.Column("instructions", sa.Text, nullable=True),
            sa.Column("icon", sa.String(16), nullable=True),
            sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("tool_policy", sa.JSON, nullable=True),
            sa.Column("model_policy", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    cols = {c["name"] for c in _inspector().get_columns("background_tasks")}
    if "agent_id" not in cols:
        op.add_column("background_tasks", sa.Column("agent_id", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("background_tasks", "agent_id")
    op.drop_table("agents")
