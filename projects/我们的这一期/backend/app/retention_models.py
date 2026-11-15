"""Content-free deletion tombstones and retryable, exact file manifests."""
from sqlalchemy import Boolean, Float, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class Retention(Base):
    __tablename__ = 'episode_retention'
    episode_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    files: Mapped[list] = mapped_column(JSON, default=list)
    contents_cleared: Mapped[bool] = mapped_column(Boolean, default=False)
