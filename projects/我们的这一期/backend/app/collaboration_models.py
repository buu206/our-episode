"""Durable collaboration records; a participant is a bearer-code identity."""

from sqlalchemy import Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now, uid


class Invitation(Base):
    __tablename__ = "collab_invitations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    code_hash: Mapped[str] = mapped_column(String, unique=True)
    expires_at: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    context_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    context_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Participant(Base):
    __tablename__ = "collab_participants"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    invite_id: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ParticipantSession(Base):
    __tablename__ = "collab_sessions"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    participant_id: Mapped[str] = mapped_column(String, index=True)
    csrf: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float] = mapped_column(Float)


class Submission(Base):
    __tablename__ = "collab_submissions"
    __table_args__ = (UniqueConstraint("participant_id", "client_request_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    invite_id: Mapped[str] = mapped_column(String, index=True)
    participant_id: Mapped[str] = mapped_column(String, index=True)
    client_request_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="draft")
    text: Mapped[str] = mapped_column(Text, default="")
    question_id: Mapped[str | None] = mapped_column(String, nullable=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    source_path: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0)
    preview_path: Mapped[str | None] = mapped_column(String, nullable=True)
    preview_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    upload_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ExpressionRevision(Base):
    __tablename__ = "collab_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    submission_id: Mapped[str] = mapped_column(String, index=True)
    participant_id: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[float] = mapped_column(Float, default=0)
    end: Mapped[float] = mapped_column(Float, default=0)
    display_name: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="proposed")
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Adoption(Base):
    __tablename__ = "collab_adoptions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    revision_id: Mapped[str] = mapped_column(String, unique=True)
    submission_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    asset_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Objection(Base):
    __tablename__ = "collab_objections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    invite_id: Mapped[str] = mapped_column(String, index=True)
    participant_id: Mapped[str] = mapped_column(String, index=True)
    submission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    start: Mapped[float | None] = mapped_column(Float, nullable=True)
    end: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="open")
    created_at: Mapped[str] = mapped_column(String, default=now)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)
