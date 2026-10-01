"""Phase 4: background_tasks.desktop_context."""
import sqlalchemy as sa
from alembic import op

revision = "0004_desktop_context"
down_revision = "0003_waiting_for"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("background_tasks")}
    if "desktop_context" not in cols:
        op.add_column("background_tasks", sa.Column("desktop_context", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("background_tasks", "desktop_context")
