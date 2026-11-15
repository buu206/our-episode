"""Persist actual model conversations and explicitly accepted actions."""
from alembic import op
import sqlalchemy as sa

revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('director_chat_turns',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('episode_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('client_id', sa.String(), nullable=False),
        sa.Column('request_hash', sa.String(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('reply', sa.Text(), nullable=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('context', sa.JSON(), nullable=False),
        sa.Column('proposal', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.UniqueConstraint('episode_id', 'client_id', name='uq_director_chat_client'))
    op.create_index('ix_director_chat_turns_episode_id', 'director_chat_turns', ['episode_id'])
    op.create_index('ix_director_chat_turns_owner_id', 'director_chat_turns', ['owner_id'])


def downgrade():
    raise RuntimeError('不自动删除已保存的导演对话与修改确认。')

