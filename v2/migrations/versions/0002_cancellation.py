"""Add control-plane cancellation intent to analysis attempts.

Cancellation is authoritative control-plane state (see :class:`AnalysisRepository`).
Adds ``analysis_attempts.cancel_requested`` so the API can request cancellation
and a worker on a different host can observe it, without any shared filesystem
marker. The column defaults to ``false`` so existing rows migrate cleanly.

Revision ID: 0002_cancellation
Revises: 0001_initial
Create Date: 2026-08-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_cancellation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_attempts",
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("analysis_attempts", "cancel_requested")
