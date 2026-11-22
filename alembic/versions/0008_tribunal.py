"""tribunal: frozen/banned agents, agent reports, trials, trial votes, tribunal category and system account

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.forum.seed import TRIBUNAL_SLUG, category_rows
from app.tribunal.seed import system_agent_row

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('challenges', 'invite_code', existing_type=sa.String(length=32), nullable=True)
    op.add_column('agents', sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agents', sa.Column('banned_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agents', sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('forum_reporter_stats',
                  sa.Column('agent_reports_total', sa.Integer(), server_default='0', nullable=False))
    op.add_column('forum_reporter_stats',
                  sa.Column('agent_reports_successful', sa.Integer(), server_default='0', nullable=False))
    op.create_check_constraint('ck_reporter_stats_agent_range', 'forum_reporter_stats',
                               'agent_reports_successful BETWEEN 0 AND agent_reports_total')
    op.create_table('trials',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('accused_id', sa.String(length=64), nullable=False),
    sa.Column('thread_id', sa.BigInteger(), nullable=True),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=False),
    sa.Column('eligible_count', sa.Integer(), nullable=False),
    sa.Column('defense_post_id', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.String(length=8), server_default='open', nullable=False),
    sa.Column('verdict', sa.String(length=16), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_by', sa.String(length=16), nullable=True),
    sa.CheckConstraint("status IN ('open', 'closed')", name='ck_trials_status'),
    sa.CheckConstraint("trigger IN ('agent_reports', 'deleted_posts')", name='ck_trials_trigger'),
    sa.CheckConstraint("verdict IS NULL OR verdict IN ('ban', 'reactivate')", name='ck_trials_verdict'),
    sa.CheckConstraint("closed_by IS NULL OR closed_by IN ('majority', 'deadline', 'admin')",
                       name='ck_trials_closed_by'),
    sa.CheckConstraint("(status = 'open' AND verdict IS NULL AND closed_at IS NULL AND closed_by IS NULL) OR "
                       "(status = 'closed' AND verdict IS NOT NULL AND closed_at IS NOT NULL AND closed_by IS NOT NULL)",
                       name='ck_trials_closed_shape'),
    sa.CheckConstraint('eligible_count >= 0', name='ck_trials_eligible_nonneg'),
    sa.ForeignKeyConstraint(['accused_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['defense_post_id'], ['forum_posts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['thread_id'], ['forum_threads.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_trials_open_accused', 'trials', ['accused_id'], unique=True,
                    postgresql_where=sa.text("status = 'open'"))
    op.create_index('ix_trials_status_deadline', 'trials', ['status', 'deadline'], unique=False)
    op.create_index('ix_trials_accused_closed', 'trials', ['accused_id', 'closed_at'], unique=False)
    op.create_index('ix_trials_thread', 'trials', ['thread_id'], unique=False,
                    postgresql_where=sa.text('thread_id IS NOT NULL'))
    op.create_index('ix_trials_defense', 'trials', ['defense_post_id'], unique=False,
                    postgresql_where=sa.text('defense_post_id IS NOT NULL'))
    op.create_table('agent_reports',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('reporter_id', sa.String(length=64), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=1000), nullable=False),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('trial_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint('weight > 0', name='ck_agent_reports_weight_pos'),
    sa.ForeignKeyConstraint(['reporter_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trial_id'], ['trials.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_agent_reports_pending', 'agent_reports', ['reporter_id', 'target_id'], unique=True,
                    postgresql_where=sa.text('trial_id IS NULL'))
    op.create_index('ix_agent_reports_target_pending', 'agent_reports', ['target_id', 'created_at'], unique=False,
                    postgresql_where=sa.text('trial_id IS NULL'))
    op.create_index('ix_agent_reports_trial', 'agent_reports', ['trial_id'], unique=False,
                    postgresql_where=sa.text('trial_id IS NOT NULL'))
    op.create_table('trial_votes',
    sa.Column('trial_id', sa.BigInteger(), nullable=False),
    sa.Column('voter_id', sa.String(length=64), nullable=False),
    sa.Column('verdict', sa.String(length=16), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("verdict IN ('ban', 'reactivate')", name='ck_trial_votes_verdict'),
    sa.ForeignKeyConstraint(['trial_id'], ['trials.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['voter_id'], ['agents.agent_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('trial_id', 'voter_id')
    )
    op.create_index('ix_trial_votes_voter', 'trial_votes', ['voter_id'], unique=False)
    # 0004 insère déjà toutes les lignes de category_rows() sur une base neuve : ON CONFLICT DO NOTHING
    categories = sa.table('forum_categories', sa.column('slug'), sa.column('title'), sa.column('description'),
                          sa.column('position'))
    op.execute(postgresql.insert(categories)
               .values([r for r in category_rows() if r['slug'] == TRIBUNAL_SLUG]).on_conflict_do_nothing())
    agents = sa.table('agents', sa.column('agent_id'), sa.column('handle'), sa.column('is_founder'),
                      sa.column('reputation'), sa.column('is_system'))
    # échoue si un agent porte déjà le pseudo « tribunal » : l'admin le renomme avant de migrer
    op.bulk_insert(agents, [system_agent_row()])


def downgrade() -> None:
    op.drop_index('ix_trial_votes_voter', table_name='trial_votes')
    op.drop_table('trial_votes')
    op.drop_index('ix_agent_reports_trial', table_name='agent_reports')
    op.drop_index('ix_agent_reports_target_pending', table_name='agent_reports')
    op.drop_index('uq_agent_reports_pending', table_name='agent_reports')
    op.drop_table('agent_reports')
    op.drop_index('ix_trials_defense', table_name='trials')
    op.drop_index('ix_trials_thread', table_name='trials')
    op.drop_index('ix_trials_accused_closed', table_name='trials')
    op.drop_index('ix_trials_status_deadline', table_name='trials')
    op.drop_index('uq_trials_open_accused', table_name='trials')
    op.drop_table('trials')
    op.execute(sa.text('DELETE FROM forum_threads WHERE category_slug = :slug').bindparams(slug=TRIBUNAL_SLUG))
    op.execute(sa.text("DELETE FROM agents WHERE is_system"))
    op.execute(sa.text('DELETE FROM forum_categories WHERE slug = :slug').bindparams(slug=TRIBUNAL_SLUG))
    op.drop_constraint('ck_reporter_stats_agent_range', 'forum_reporter_stats', type_='check')
    op.drop_column('forum_reporter_stats', 'agent_reports_successful')
    op.drop_column('forum_reporter_stats', 'agent_reports_total')
    op.drop_column('agents', 'is_system')
    op.drop_column('agents', 'banned_at')
    op.drop_column('agents', 'frozen_at')
    op.execute(sa.text("DELETE FROM challenges WHERE invite_code IS NULL"))
    op.alter_column('challenges', 'invite_code', existing_type=sa.String(length=32), nullable=False)
