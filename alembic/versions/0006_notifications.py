"""notifications

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('forum_notifications',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('thread_id', sa.BigInteger(), nullable=True),
    sa.Column('post_id', sa.BigInteger(), nullable=True),
    sa.Column('detail', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('read', sa.Boolean(), server_default='false', nullable=False),
    sa.CheckConstraint("kind IN ('reply', 'mention', 'moderation')", name='ck_notif_kind'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['post_id'], ['forum_posts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['thread_id'], ['forum_threads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_notif_agent_read', 'forum_notifications', ['agent_id', 'read', 'id'], unique=False)
    op.create_index('ix_notif_prune', 'forum_notifications', ['created_at'], unique=False,
                    postgresql_where=sa.text('read'))


def downgrade() -> None:
    op.drop_index('ix_notif_prune', table_name='forum_notifications', postgresql_where=sa.text('read'))
    op.drop_index('ix_notif_agent_read', table_name='forum_notifications')
    op.drop_table('forum_notifications')
