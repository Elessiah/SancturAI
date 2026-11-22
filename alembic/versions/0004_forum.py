"""forum

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.forum.seed import category_rows

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('forum_categories',
    sa.Column('slug', sa.String(length=48), nullable=False),
    sa.Column('title', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('slug')
    )
    op.create_table('forum_threads',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('category_slug', sa.String(length=48), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('author_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reply_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('score', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['category_slug'], ['forum_categories.slug']),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_forum_threads_author_id'), 'forum_threads', ['author_id'], unique=False)
    op.create_index('ix_threads_cat_activity', 'forum_threads', ['category_slug', 'last_activity_at', 'id'], unique=False)
    op.create_index('ix_threads_cat_created', 'forum_threads', ['category_slug', 'created_at', 'id'], unique=False)
    op.create_index('ix_threads_cat_score', 'forum_threads', ['category_slug', 'score', 'id'], unique=False)
    op.create_table('forum_posts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('thread_id', sa.BigInteger(), nullable=False),
    sa.Column('author_id', sa.String(length=64), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('score', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_first', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('search', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', body)", persisted=True), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['thread_id'], ['forum_threads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_forum_posts_author_id'), 'forum_posts', ['author_id'], unique=False)
    op.create_index('ix_posts_search', 'forum_posts', ['search'], unique=False, postgresql_using='gin')
    op.create_index('ix_posts_thread_id', 'forum_posts', ['thread_id', 'id'], unique=False)
    op.bulk_insert(sa.table("forum_categories", sa.column("slug"), sa.column("title"),
                            sa.column("description"), sa.column("position")), category_rows())


def downgrade() -> None:
    op.drop_index('ix_posts_thread_id', table_name='forum_posts')
    op.drop_index('ix_posts_search', table_name='forum_posts', postgresql_using='gin')
    op.drop_index(op.f('ix_forum_posts_author_id'), table_name='forum_posts')
    op.drop_table('forum_posts')
    op.drop_index('ix_threads_cat_score', table_name='forum_threads')
    op.drop_index('ix_threads_cat_created', table_name='forum_threads')
    op.drop_index('ix_threads_cat_activity', table_name='forum_threads')
    op.drop_index(op.f('ix_forum_threads_author_id'), table_name='forum_threads')
    op.drop_table('forum_threads')
    op.drop_table('forum_categories')
