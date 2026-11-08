"""Add evidence review, immutable narration and durable model accounting."""
from alembic import op
import sqlalchemy as sa

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade():
    for name, datatype, default in (
        ('evidence_revision', sa.Integer(), '0'),
        ('analysis_state', sa.String(), 'empty'),
        ('analysis_asset_ids', sa.JSON(), '[]'),
        ('analysis_notes', sa.JSON(), '[]'),
        ('analysis_coverage', sa.JSON(), '[]'),
    ):
        op.add_column('episodes', sa.Column(name, datatype, nullable=False, server_default=default))
    op.add_column('episodes', sa.Column('evidence_confirmed_revision', sa.Integer()))
    op.add_column('episodes', sa.Column('directions_revision', sa.Integer()))
    for name, datatype, default in (
        ('narration_text', sa.Text(), ''), ('narration_approved', sa.Boolean(), '0'),
        ('packaging', sa.String(), 'standard'),
    ):
        op.add_column('versions', sa.Column(name, datatype, nullable=False, server_default=default))
    for name, datatype in (('evidence_revision', sa.Integer()), ('direction_id', sa.String()), ('narration_path', sa.String())):
        op.add_column('versions', sa.Column(name, datatype))
    op.create_table('model_calls', sa.Column('id',sa.String(),primary_key=True),
        sa.Column('owner_id',sa.String(),nullable=False),sa.Column('episode_id',sa.String(),nullable=False),
        sa.Column('job_id',sa.String(),nullable=False),sa.Column('reservation_id',sa.String(),nullable=False,unique=True),
        sa.Column('kind',sa.String(),nullable=False),sa.Column('request_digest',sa.String()),
        sa.Column('state',sa.String(),nullable=False),sa.Column('usage',sa.JSON(),nullable=False),
        sa.Column('elapsed',sa.Float()),sa.Column('created_at',sa.String(),nullable=False))
    for name in ('owner_id','episode_id','job_id'):
        op.create_index('ix_model_calls_'+name,'model_calls',[name])


def downgrade():
    raise RuntimeError('保留已有证据与费用数据，不自动删除业务记录。')
