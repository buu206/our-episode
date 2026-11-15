"""Add retention metadata without deleting or enrolling existing content."""
from alembic import op
import sqlalchemy as sa

revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('episode_retention',
        sa.Column('episode_id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('expires_at', sa.Float()), sa.Column('requested_at', sa.Float()),
        sa.Column('completed_at', sa.Float()), sa.Column('reason', sa.String()),
        sa.Column('files', sa.JSON(), nullable=False),
        sa.Column('contents_cleared', sa.Boolean(), nullable=False))
    op.create_index('ix_episode_retention_owner_id', 'episode_retention', ['owner_id'])


def downgrade():
    raise RuntimeError('不自动移除删除状态和待清理文件清单。')
