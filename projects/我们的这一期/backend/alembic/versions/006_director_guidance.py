"""Persist episode requirements without changing old versions or retention."""
from alembic import op
import sqlalchemy as sa

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('director_guidance',
        sa.Column('episode_id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('focus', sa.String(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('messages', sa.JSON(), nullable=False),
        sa.Column('operations', sa.JSON(), nullable=False),
        sa.Column('directions_brief_revision', sa.Integer(), nullable=True))
    op.create_index('ix_director_guidance_owner_id', 'director_guidance', ['owner_id'])


def downgrade():
    raise RuntimeError('不自动删除已保存的导演需求与确认记录。')
