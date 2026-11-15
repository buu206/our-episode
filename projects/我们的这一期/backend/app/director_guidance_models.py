"""Episode-owned requirements and deterministic guidance; never model chat."""
from sqlalchemy import Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class DirectorGuidance(Base):
    __tablename__ = 'director_guidance'
    episode_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    focus: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default='')
    messages: Mapped[list] = mapped_column(JSON, default=list)
    operations: Mapped[list] = mapped_column(JSON, default=list)
    directions_brief_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
