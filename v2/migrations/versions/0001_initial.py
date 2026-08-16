"""Initial FootballAI P2 control-plane schema.

Creates the analysis-attempt lifecycle tables from the single SQLAlchemy Core
metadata definition so the migration and the runtime schema can never drift.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-16
"""

from footballai_v2.storage.postgres.schema import METADATA
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    METADATA.create_all(bind=op.get_bind())


def downgrade() -> None:
    METADATA.drop_all(bind=op.get_bind())
