"""Persist fixed-version viewing grants, sessions and idempotent owner actions."""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shares", sa.Column("id",sa.String(),primary_key=True),
        sa.Column("owner_id",sa.String(),nullable=False),sa.Column("episode_id",sa.String(),nullable=False),
        sa.Column("version_id",sa.String(),nullable=False),sa.Column("label",sa.String(),nullable=False),
        sa.Column("title",sa.String(),nullable=False),sa.Column("code_hash",sa.String(),nullable=False,unique=True),
        sa.Column("state",sa.String(),nullable=False),sa.Column("revision",sa.Integer(),nullable=False),
        sa.Column("output_path",sa.String(),nullable=False),sa.Column("confirmed_at",sa.String(),nullable=False),
        sa.Column("fingerprint",sa.String(),nullable=False),sa.Column("media_sha256",sa.String(),nullable=False),
        sa.Column("media_size",sa.Integer(),nullable=False),sa.Column("expires_at",sa.Float(),nullable=False),
        sa.Column("closed_at",sa.Float()),sa.Column("restricted_reason",sa.JSON()),
        sa.Column("created_at",sa.String(),nullable=False),sa.Column("updated_at",sa.String(),nullable=False))
    for name in ("owner_id","episode_id","version_id"):
        op.create_index("ix_shares_"+name,"shares",[name])
    op.create_table("viewer_sessions",sa.Column("token_hash",sa.String(),primary_key=True),
        sa.Column("share_id",sa.String(),nullable=False),sa.Column("csrf",sa.String(),nullable=False),
        sa.Column("expires_at",sa.Float(),nullable=False))
    op.create_index("ix_viewer_sessions_share_id","viewer_sessions",["share_id"])
    op.create_table("share_operations",sa.Column("id",sa.String(),primary_key=True),
        sa.Column("owner_id",sa.String(),nullable=False),sa.Column("client_request_id",sa.String(),nullable=False),
        sa.Column("kind",sa.String(),nullable=False),sa.Column("request_hash",sa.String(),nullable=False),
        sa.Column("share_id",sa.String(),nullable=False),sa.Column("result",sa.JSON(),nullable=False),
        sa.Column("created_at",sa.String(),nullable=False),sa.UniqueConstraint("owner_id","client_request_id"))
    for name in ("owner_id","share_id"):
        op.create_index("ix_share_operations_"+name,"share_operations",[name])


def downgrade():
    raise RuntimeError("不自动删除分享、关闭记录与观看会话。")
