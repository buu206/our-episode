"""Fixed-version viewing grants; all credentials are stored as hashes."""
from sqlalchemy import Float, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, now, uid


class Share(Base):
    __tablename__ = "shares"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    version_id: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    code_hash: Mapped[str] = mapped_column(String, unique=True)
    state: Mapped[str] = mapped_column(String, default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    output_path: Mapped[str] = mapped_column(String)
    confirmed_at: Mapped[str] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String)
    media_sha256: Mapped[str] = mapped_column(String)
    media_size: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    restricted_reason: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    updated_at: Mapped[str] = mapped_column(String, default=now)


class ViewerSession(Base):
    __tablename__ = "viewer_sessions"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    share_id: Mapped[str] = mapped_column(String, index=True)
    csrf: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float] = mapped_column(Float)


class ShareOperation(Base):
    __tablename__ = "share_operations"
    __table_args__ = (UniqueConstraint("owner_id", "client_request_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    client_request_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    share_id: Mapped[str] = mapped_column(String, index=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
