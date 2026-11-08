from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Episode(Base):
    __tablename__ = 'episodes'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String(120))
    background: Mapped[str] = mapped_column(Text, default='')
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    directions: Mapped[list] = mapped_column(JSON, default=list)
    selected_direction: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_revision: Mapped[int] = mapped_column(Integer, default=0)
    evidence_confirmed_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_state: Mapped[str] = mapped_column(String, default='empty')
    analysis_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    analysis_notes: Mapped[list] = mapped_column(JSON, default=list)
    analysis_coverage: Mapped[list] = mapped_column(JSON, default=list)
    directions_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Asset(Base):
    __tablename__ = 'assets'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    filename: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    has_audio: Mapped[bool] = mapped_column(Boolean)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Version(Base):
    __tablename__ = 'versions'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String(120))
    origin: Mapped[str] = mapped_column(String, default='manual')
    state: Mapped[str] = mapped_column(String, default='draft')
    segments: Mapped[list] = mapped_column(JSON)
    base_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    narration_text: Mapped[str] = mapped_column(Text, default='')
    narration_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    narration_path: Mapped[str | None] = mapped_column(String, nullable=True)
    packaging: Mapped[str] = mapped_column(String, default='standard')
    created_at: Mapped[str] = mapped_column(String, default=now)


class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (UniqueConstraint('owner_id', 'idempotency_key'),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default='queued')
    stage: Mapped[str] = mapped_column(String, default='等待处理')
    idempotency_key: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Session(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    csrf: Mapped[str] = mapped_column(String)
    expires: Mapped[float] = mapped_column(Float)


class Budget(Base):
    __tablename__ = 'budget'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    # Integer micro-yuan avoids rounding or float comparisons at the limit.
    amount: Mapped[int] = mapped_column(Integer)
    cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String, default='reserved')
    created_at: Mapped[str] = mapped_column(String, default=now)


class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ModelCall(Base):
    __tablename__ = 'model_calls'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    reservation_id: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)
    request_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default='prepared')
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    elapsed: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
