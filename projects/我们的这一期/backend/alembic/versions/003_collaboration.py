"""Persist restricted invitations, personal drafts, consent and scoped objections."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    def table(name, fields, uniques=(), indexes=()):
        columns = [sa.Column("id", sa.String(), primary_key=True)]
        for key, datatype, nullable in fields:
            columns.append(sa.Column(key, datatype, nullable=nullable))
        op.create_table(name, *columns, *(sa.UniqueConstraint(*keys) for keys in uniques))
        for key in indexes:
            op.create_index("ix_" + name + "_" + key, name, [key])
    S, T, F, I, J = sa.String, sa.Text, sa.Float, sa.Integer, sa.JSON
    table("collab_invitations", [("owner_id",S(),False),("episode_id",S(),False),("role",S(),False),
          ("label",S(),False),("code_hash",S(),False),("expires_at",F(),False),("closed_at",F(),True),
          ("context",J(),True),("questions",J(),False),("context_job_id",S(),True),("context_path",S(),True),
          ("created_at",S(),False)], [("code_hash",)], ["owner_id","episode_id"])
    table("collab_participants", [("invite_id",S(),False),("display_name",S(),False),("created_at",S(),False)], [("invite_id",)])
    op.create_table("collab_sessions", sa.Column("token_hash",S(),primary_key=True),
                    sa.Column("participant_id",S(),nullable=False),sa.Column("csrf",S(),nullable=False),
                    sa.Column("expires_at",F(),nullable=False))
    op.create_index("ix_collab_sessions_participant_id","collab_sessions",["participant_id"])
    table("collab_submissions", [("owner_id",S(),False),("episode_id",S(),False),("invite_id",S(),False),
          ("participant_id",S(),False),("client_request_id",S(),False),("request_hash",S(),True),
          ("kind",S(),False),("state",S(),False),("text",T(),False),("question_id",S(),True),
          ("filename",S(),True),("source_path",S(),True),("size",I(),False),("duration",F(),False),
          ("preview_path",S(),True),("preview_job_id",S(),True),("upload_job_id",S(),True),
          ("deleted_at",F(),True),("created_at",S(),False)], [("participant_id","client_request_id")],
          ["owner_id","episode_id","invite_id","participant_id"])
    table("collab_revisions", [("submission_id",S(),False),("participant_id",S(),False),("text",T(),False),
          ("start",F(),False),("end",F(),False),("display_name",S(),False),("state",S(),False),
          ("confirmed_at",S(),True),("created_at",S(),False)], indexes=["submission_id","participant_id"])
    table("collab_adoptions", [("owner_id",S(),False),("episode_id",S(),False),("revision_id",S(),False),
          ("submission_id",S(),False),("state",S(),False),("asset_id",S(),True),("job_id",S(),True),
          ("output_path",S(),True),("created_at",S(),False)], [("revision_id",),("asset_id",)],
          ["owner_id","episode_id","submission_id"])
    table("collab_objections", [("owner_id",S(),False),("episode_id",S(),False),("invite_id",S(),False),
          ("participant_id",S(),False),("submission_id",S(),True),("asset_id",S(),True),("start",F(),True),
          ("end",F(),True),("reason",T(),False),("state",S(),False),("created_at",S(),False),
          ("resolved_at",S(),True)], indexes=["owner_id","episode_id","invite_id","participant_id"])


def downgrade():
    raise RuntimeError("不自动删除投稿、本人确认和异议记录。")
