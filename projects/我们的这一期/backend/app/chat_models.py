"""Episode-owned model conversations and reviewable actions."""
from sqlalchemy import JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, now, uid


class DirectorChatTurn(Base):
    __tablename__ = 'director_chat_turns'
    __table_args__ = (UniqueConstraint('episode_id', 'client_id', name='uq_director_chat_client'),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    episode_id: Mapped[str] = mapped_column(String, index=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    client_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    question: Mapped[str] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String)
    job_id: Mapped[str] = mapped_column(String)
    snapshot: Mapped[dict] = mapped_column(JSON)
    context: Mapped[dict] = mapped_column(JSON)
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)

