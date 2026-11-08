"""Initial local single-creator schema."""
from alembic import op
import sqlalchemy as sa

revision = '001'
down_revision = None
branch_labels = depends_on = None


def upgrade():
    # Schema is frozen here rather than importing mutable application models.
    op.create_table('episodes', sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('title',sa.String(120),nullable=False),sa.Column('background',sa.Text(),nullable=False),sa.Column('evidence',sa.JSON(),nullable=False),sa.Column('directions',sa.JSON(),nullable=False),sa.Column('selected_direction',sa.String()),sa.Column('created_at',sa.String(),nullable=False))
    op.create_table('assets',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('episode_id',sa.String(),nullable=False),sa.Column('filename',sa.String(200),nullable=False),sa.Column('path',sa.String(),nullable=False),sa.Column('size',sa.Integer(),nullable=False),sa.Column('duration',sa.Float(),nullable=False),sa.Column('width',sa.Integer(),nullable=False),sa.Column('height',sa.Integer(),nullable=False),sa.Column('has_audio',sa.Boolean(),nullable=False),sa.Column('deleted',sa.Boolean(),nullable=False),sa.Column('created_at',sa.String(),nullable=False))
    op.create_table('versions',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('episode_id',sa.String(),nullable=False),sa.Column('title',sa.String(120),nullable=False),sa.Column('origin',sa.String(),nullable=False),sa.Column('state',sa.String(),nullable=False),sa.Column('segments',sa.JSON(),nullable=False),sa.Column('base_version_id',sa.String()),sa.Column('output_path',sa.String()),sa.Column('job_id',sa.String()),sa.Column('error',sa.JSON()),sa.Column('confirmed_at',sa.String()),sa.Column('created_at',sa.String(),nullable=False))
    op.create_table('jobs',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('episode_id',sa.String(),nullable=False),sa.Column('version_id',sa.String()),sa.Column('kind',sa.String(),nullable=False),sa.Column('state',sa.String(),nullable=False),sa.Column('stage',sa.String(),nullable=False),sa.Column('idempotency_key',sa.String(),nullable=False),sa.Column('payload',sa.JSON(),nullable=False),sa.Column('result',sa.JSON()),sa.Column('error',sa.JSON()),sa.Column('created_at',sa.String(),nullable=False),sa.UniqueConstraint('owner_id','idempotency_key'))
    op.create_table('sessions',sa.Column('token_hash',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('csrf',sa.String(),nullable=False),sa.Column('expires',sa.Float(),nullable=False))
    op.create_table('budget',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('episode_id',sa.String(),nullable=False),sa.Column('amount',sa.Integer(),nullable=False),sa.Column('cost',sa.Integer()),sa.Column('state',sa.String(),nullable=False),sa.Column('created_at',sa.String(),nullable=False))
    op.create_table('audit',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('action',sa.String(),nullable=False),sa.Column('target_id',sa.String(),nullable=False),sa.Column('details',sa.JSON(),nullable=False),sa.Column('created_at',sa.String(),nullable=False))
    for table in ('episodes','assets','versions','jobs','sessions','budget'):
        op.create_index('ix_'+table+'_owner_id',table,['owner_id'])
    for table in ('assets','versions','jobs','budget'):
        op.create_index('ix_'+table+'_episode_id',table,['episode_id'])


def downgrade():
    raise RuntimeError('自动删除业务数据的降级已禁用；请先备份并审批迁移。')
