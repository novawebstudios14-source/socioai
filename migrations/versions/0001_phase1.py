"""Create the Phase 1 multi-tenant schema.

Revision ID: 0001_phase1
"""
from alembic import op
from socioai.database import Base
from socioai import models  # noqa: F401

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())

