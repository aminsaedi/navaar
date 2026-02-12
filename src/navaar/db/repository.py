from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from navaar.db.models import SyncLog, SyncState, Track


class TrackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create_track(self, **kwargs: object) -> Track:
        async with self._sf() as session:
            track = Track(**kwargs)
            session.add(track)
            await session.commit()
            await session.refresh(track)
            return track

    async def get_track(self, track_id: int) -> Track | None:
        async with self._sf() as session:
            return await session.get(Track, track_id)

    async def get_track_by_tg_file_unique_id(self, file_unique_id: str) -> Track | None:
        async with self._sf() as session:
            result = await session.execute(
                select(Track).where(Track.tg_file_unique_id == file_unique_id)
            )
            return result.scalar_one_or_none()

    async def get_track_by_tg_message_id(self, message_id: int) -> Track | None:
        async with self._sf() as session:
            result = await session.execute(
                select(Track).where(Track.tg_message_id == message_id)
            )
            return result.scalar_one_or_none()

    async def get_track_by_yt_video_id(self, video_id: str) -> Track | None:
        async with self._sf() as session:
            result = await session.execute(
                select(Track).where(Track.yt_video_id == video_id)
            )
            return result.scalar_one_or_none()

    async def get_pending_tracks(self, direction: str) -> list[Track]:
        async with self._sf() as session:
            result = await session.execute(
                select(Track).where(
                    Track.direction == direction,
                    Track.status.in_(["pending", "retry_scheduled"]),
                )
            )
            return list(result.scalars().all())

    async def get_failed_tracks(self, direction: str | None = None) -> list[Track]:
        async with self._sf() as session:
            stmt = select(Track).where(Track.status == "failed")
            if direction:
                stmt = stmt.where(Track.direction == direction)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_track(self, track_id: int, **kwargs: object) -> Track | None:
        async with self._sf() as session:
            await session.execute(
                update(Track).where(Track.id == track_id).values(**kwargs)
            )
            await session.commit()
            return await session.get(Track, track_id)

    async def mark_synced(self, track_id: int, **extra: object) -> Track | None:
        return await self.update_track(
            track_id,
            status="synced",
            synced_at=datetime.now(timezone.utc),
            **extra,
        )

    async def mark_failed(self, track_id: int, reason: str) -> Track | None:
        track = await self.get_track(track_id)
        if not track:
            return None
        return await self.update_track(
            track_id,
            status="failed",
            failure_reason=reason,
            retry_count=track.retry_count + 1,
        )

    async def mark_duplicate(self, track_id: int) -> Track | None:
        return await self.update_track(track_id, status="duplicate")

    async def reset_for_retry(self, track_id: int) -> Track | None:
        return await self.update_track(
            track_id,
            status="retry_scheduled",
            failure_reason=None,
        )

    async def reset_all_failed(self, direction: str | None = None) -> int:
        async with self._sf() as session:
            stmt = (
                update(Track)
                .where(Track.status == "failed")
                .values(status="retry_scheduled", failure_reason=None)
            )
            if direction:
                stmt = stmt.where(Track.direction == direction)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount  # type: ignore[return-value]

    async def get_counts(self) -> dict[str, dict[str, int]]:
        async with self._sf() as session:
            result = await session.execute(
                select(Track.direction, Track.status, func.count(Track.id)).group_by(
                    Track.direction, Track.status
                )
            )
            counts: dict[str, dict[str, int]] = {}
            for direction, status, count in result:
                counts.setdefault(direction, {})[status] = count
            return counts

    async def get_stats(self) -> dict[str, object]:
        async with self._sf() as session:
            total = (await session.execute(select(func.count(Track.id)))).scalar() or 0
            synced = (
                await session.execute(
                    select(func.count(Track.id)).where(Track.status == "synced")
                )
            ).scalar() or 0
            failed = (
                await session.execute(
                    select(func.count(Track.id)).where(Track.status == "failed")
                )
            ).scalar() or 0
            duplicates = (
                await session.execute(
                    select(func.count(Track.id)).where(Track.status == "duplicate")
                )
            ).scalar() or 0
            pending = (
                await session.execute(
                    select(func.count(Track.id)).where(
                        Track.status.in_(["pending", "retry_scheduled"])
                    )
                )
            ).scalar() or 0
            tg_to_yt = (
                await session.execute(
                    select(func.count(Track.id)).where(
                        Track.direction == "tg_to_yt", Track.status == "synced"
                    )
                )
            ).scalar() or 0
            yt_to_tg = (
                await session.execute(
                    select(func.count(Track.id)).where(
                        Track.direction == "yt_to_tg", Track.status == "synced"
                    )
                )
            ).scalar() or 0
            return {
                "total": total,
                "synced": synced,
                "failed": failed,
                "duplicates": duplicates,
                "pending": pending,
                "tg_to_yt_synced": tg_to_yt,
                "yt_to_tg_synced": yt_to_tg,
                "success_rate": round(synced / total * 100, 1) if total > 0 else 0.0,
            }

    async def get_recent_tracks(self, limit: int = 10, direction: str | None = None) -> list[Track]:
        async with self._sf() as session:
            stmt = select(Track).order_by(Track.id.desc()).limit(limit)
            if direction:
                stmt = stmt.where(Track.direction == direction)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def delete_track(self, track_id: int) -> bool:
        async with self._sf() as session:
            track = await session.get(Track, track_id)
            if not track:
                return False
            await session.delete(track)
            await session.commit()
            return True


class SyncStateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, key: str) -> str | None:
        async with self._sf() as session:
            state = await session.get(SyncState, key)
            return state.value if state else None

    async def set(self, key: str, value: str) -> None:
        async with self._sf() as session:
            state = await session.get(SyncState, key)
            if state:
                state.value = value
            else:
                session.add(SyncState(key=key, value=value))
            await session.commit()

    async def get_json(self, key: str) -> object:
        raw = await self.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: object) -> None:
        await self.set(key, json.dumps(value))


class SyncLogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def log(
        self,
        event: str,
        track_id: int | None = None,
        direction: str | None = None,
        details: dict | None = None,
    ) -> SyncLog:
        async with self._sf() as session:
            entry = SyncLog(
                track_id=track_id,
                event=event,
                direction=direction,
                details=json.dumps(details) if details else None,
            )
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            return entry

    async def get_logs_for_track(self, track_id: int, limit: int = 10) -> list[SyncLog]:
        async with self._sf() as session:
            result = await session.execute(
                select(SyncLog)
                .where(SyncLog.track_id == track_id)
                .order_by(SyncLog.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_recent_logs(self, limit: int = 20) -> list[SyncLog]:
        async with self._sf() as session:
            result = await session.execute(
                select(SyncLog).order_by(SyncLog.id.desc()).limit(limit)
            )
            return list(result.scalars().all())
