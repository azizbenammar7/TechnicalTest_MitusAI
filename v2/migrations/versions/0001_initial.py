"""Initial FootballAI P2 control-plane schema, frozen at revision creation.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "logical_analyses",
        sa.Column("logical_analysis_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("data_origin", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "analysis_attempts",
        sa.Column("run_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "logical_analysis_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("logical_analyses.logical_analysis_id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("previous_attempt_run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_origin", sa.Text(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "logical_analysis_id", "attempt_number", name="uq_attempt_per_logical"
        ),
    )
    op.create_table(
        "stage_executions",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("analysis_attempts.run_id"),
            primary_key=True,
        ),
        sa.Column("stage_id", sa.String(length=128), primary_key=True),
        sa.Column("stage_name", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("progress_percent", sa.Float(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
    )
    op.create_table(
        "artifact_metadata",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("analysis_attempts.run_id"),
            primary_key=True,
        ),
        sa.Column("artifact_id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=True),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("logical_analysis_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    # Downgrades are supported for disposable staging/dev recovery only. They
    # are destructive and must never be run against a persistent environment
    # without an operator-reviewed backup and rollback plan.
    op.drop_table("audit_events")
    op.drop_table("artifact_metadata")
    op.drop_table("stage_executions")
    op.drop_table("analysis_attempts")
    op.drop_table("logical_analyses")
