"""SQLAlchemy Core schema for the PostgreSQL control plane.

The full ``footballai.analysis-run/v1`` manifest is stored verbatim as JSONB in
``analysis_attempts.manifest`` and remains the authoritative record. The
scalar columns and the ``stage_executions`` / ``artifact_metadata`` /
``audit_events`` tables are queryable projections derived from that manifest;
they never hold anything the manifest does not, and they never hold large
artifact bytes (only metadata). This keeps the control plane transactional and
small while the data plane (object storage) owns the bytes.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

# Bumped whenever a migration changes the control-plane schema. Readiness and
# startup compare this against the applied Alembic revision.
SCHEMA_REVISION = "0001_initial"

METADATA = MetaData()

logical_analyses = Table(
    "logical_analyses",
    METADATA,
    Column("logical_analysis_id", UUID(as_uuid=False), primary_key=True),
    Column("data_origin", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

analysis_attempts = Table(
    "analysis_attempts",
    METADATA,
    Column("run_id", UUID(as_uuid=False), primary_key=True),
    Column(
        "logical_analysis_id",
        UUID(as_uuid=False),
        ForeignKey("logical_analyses.logical_analysis_id"),
        nullable=False,
    ),
    Column("attempt_number", Integer, nullable=False),
    Column("previous_attempt_run_id", UUID(as_uuid=False), nullable=True),
    Column("status", Text, nullable=False),
    Column("data_origin", Text, nullable=False),
    Column("pipeline_version", Text, nullable=False),
    Column("contract_version", Text, nullable=False),
    # Authoritative canonical AnalysisRun.to_dict() payload.
    Column("manifest", JSONB, nullable=False),
    # Monotonic optimistic-concurrency token; increments on every write.
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "logical_analysis_id", "attempt_number", name="uq_attempt_per_logical"
    ),
)

stage_executions = Table(
    "stage_executions",
    METADATA,
    Column("run_id", UUID(as_uuid=False), ForeignKey("analysis_attempts.run_id"), primary_key=True),
    Column("stage_id", String(128), primary_key=True),
    Column("stage_name", Text, nullable=False),
    Column("required", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("progress_percent", Float, nullable=False),
    Column("attempt_number", Integer, nullable=False),
)

artifact_metadata = Table(
    "artifact_metadata",
    METADATA,
    Column("run_id", UUID(as_uuid=False), ForeignKey("analysis_attempts.run_id"), primary_key=True),
    Column("artifact_id", String(128), primary_key=True),
    Column("name", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("relative_path", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("schema_version", Text, nullable=True),
)

audit_events = Table(
    "audit_events",
    METADATA,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("logical_analysis_id", UUID(as_uuid=False), nullable=False),
    Column("run_id", UUID(as_uuid=False), nullable=True),
    Column("attempt_number", Integer, nullable=True),
    Column("event_type", Text, nullable=False),
    Column("detail", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
