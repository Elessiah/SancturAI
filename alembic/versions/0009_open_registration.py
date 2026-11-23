"""open registration: an invite code no longer spawns challenges

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('ck_invites_challenges_used_range', 'invites', type_='check')
    op.drop_column('invites', 'challenges_used')


def downgrade() -> None:
    op.add_column('invites', sa.Column('challenges_used', sa.Integer(), server_default='0', nullable=False))
    op.alter_column('invites', 'challenges_used', server_default=None)
    op.create_check_constraint('ck_invites_challenges_used_range', 'invites', 'challenges_used BETWEEN 0 AND 3')
