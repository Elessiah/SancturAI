"""drop the 7-day suspension, replaced by the tribunal

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # spec visibilité §4.9 : supprimer la colonne lève les suspensions en cours
    op.drop_column('agents', 'suspended_until')


def downgrade() -> None:
    op.add_column('agents', sa.Column('suspended_until', sa.DateTime(timezone=True), nullable=True))
