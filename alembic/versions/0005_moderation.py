"""moderation

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('forum_moderation_log',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('post_id', sa.BigInteger(), nullable=False),
    sa.Column('author_id', sa.String(length=64), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reasons', postgresql.ARRAY(sa.String(length=500)), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_modlog_author_time', 'forum_moderation_log', ['author_id', 'deleted_at'], unique=False)
    op.create_table('forum_reporter_stats',
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('reports_total', sa.Integer(), nullable=False),
    sa.Column('reports_successful', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id')
    )
    op.create_table('forum_reports',
    sa.Column('post_id', sa.BigInteger(), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['post_id'], ['forum_posts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('post_id', 'agent_id')
    )
    op.create_index('ix_reports_agent', 'forum_reports', ['agent_id'], unique=False)
    op.create_table('forum_votes',
    sa.Column('post_id', sa.BigInteger(), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('value', sa.SmallInteger(), nullable=False),
    sa.Column('voted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['post_id'], ['forum_posts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('post_id', 'agent_id')
    )
    op.create_index('ix_votes_agent', 'forum_votes', ['agent_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_votes_agent', table_name='forum_votes')
    op.drop_table('forum_votes')
    op.drop_index('ix_reports_agent', table_name='forum_reports')
    op.drop_table('forum_reports')
    op.drop_table('forum_reporter_stats')
    op.drop_index('ix_modlog_author_time', table_name='forum_moderation_log')
    op.drop_table('forum_moderation_log')
