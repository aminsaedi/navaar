from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter, Histogram

from navaar.metrics import (
    SP_SEARCH_DURATION,
    SP_SEARCH_TOTAL,
    YT_SEARCH_DURATION,
    YT_SEARCH_TOTAL,
)


@dataclass(frozen=True)
class TargetAdapter:
    """Per-service knobs that differentiate the otherwise-identical push flow."""

    name: str            # "yt" | "sp"
    match_id_key: str    # key of the external id in a find_best_match() result
    match_name_key: str  # key of the display name in that result
    db_field: str        # Track column to persist the external id into
    no_match_reason: str
    search_total: Counter
    search_duration: Histogram


YT_TARGET = TargetAdapter(
    name="yt",
    match_id_key="videoId",
    match_name_key="title",
    db_field="yt_video_id",
    no_match_reason="no_yt_match",
    search_total=YT_SEARCH_TOTAL,
    search_duration=YT_SEARCH_DURATION,
)

SP_TARGET = TargetAdapter(
    name="sp",
    match_id_key="id",
    match_name_key="name",
    db_field="sp_track_id",
    no_match_reason="no_sp_match",
    search_total=SP_SEARCH_TOTAL,
    search_duration=SP_SEARCH_DURATION,
)
