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

revision = "0002_cancellation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 builds the whole schema from the live SQLAlchemy metadata, so on a
    # fresh database this column is already present (the metadata now declares
    # it). IF NOT EXISTS makes this migration reconcile both a fresh install and
    # a database created before cancellation existed, without drift. Postgres is
    # the only target, so the native guard is safe.
    op.execute(
        "ALTER TABLE analysis_attempts "
        "ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE analysis_attempts DROP COLUMN IF EXISTS cancel_requested")
