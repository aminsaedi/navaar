from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # tg_to_yt | yt_to_tg
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    artist: Mapped[str | None] = mapped_column(String(500), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    identification_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    tg_file_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tg_file_unique_id: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    yt_video_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    yt_set_video_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    logs: Mapped[list[SyncLog]] = relationship("SyncLog", back_populates="track")


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tracks.id"), nullable=True)
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(10), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    track: Mapped[Track | None] = relationship("Track", back_populates="logs")
