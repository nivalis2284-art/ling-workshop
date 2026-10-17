"""Validated owner policy for daily reflection; no model/profile lookup here."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .calendar import MAX_DATE_YEARS, MIN_DATE_YEARS, CalendarError, parse_local_time, zone

MAX_THRESHOLD_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_CALLS = 64
MAX_EVENTS = 64
MAX_DAILY_CALLS = 512


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    revision: int = 1
    local_time: str = "04:00"
    timezone: str = "Asia/Tokyo"
    min_age_days: int = 3
    threshold_bytes: int = 32768
    profile_id: str | None = None
    max_calls: int = 8
    max_source_bytes: int = 128 * 1024
    max_events: int = 6
    daily_call_limit: int = 24

    def as_dict(self) -> dict:
        return asdict(self)


def validate(
    *,
    enabled,
    revision,
    local_time,
    timezone,
    min_age_days,
    threshold_bytes,
    profile_id,
    max_calls,
    max_source_bytes,
    max_events,
    daily_call_limit,
) -> Policy:
    if type(enabled) is not bool:
        raise ValueError("reflection enabled must be boolean")
    if type(revision) is not int or revision < 1:
        raise ValueError("reflection revision must be a positive integer")
    if (
        type(min_age_days) is not int
        or not MIN_DATE_YEARS - 1 <= min_age_days <= MAX_DATE_YEARS * 366
    ):
        raise ValueError("reflection min_age_days is out of range")
    if type(threshold_bytes) is not int or not 1 <= threshold_bytes <= MAX_THRESHOLD_BYTES:
        raise ValueError("reflection threshold_bytes is out of range")
    if type(max_source_bytes) is not int or not 1 <= max_source_bytes <= MAX_SOURCE_BYTES:
        raise ValueError("reflection max_source_bytes is out of range")
    if type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS:
        raise ValueError("reflection max_calls is out of range")
    if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
        raise ValueError("reflection max_events is out of range")
    if type(daily_call_limit) is not int or not 1 <= daily_call_limit <= MAX_DAILY_CALLS:
        raise ValueError("reflection daily_call_limit is out of range")
    if max_calls > daily_call_limit:
        raise ValueError("reflection max_calls cannot exceed daily_call_limit")
    if profile_id is not None and (
        not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 200
    ):
        raise ValueError("reflection profile_id is invalid")
    try:
        parse_local_time(local_time)
        zone(timezone)
    except CalendarError as error:
        raise ValueError(str(error)) from None
    return Policy(
        enabled=enabled,
        revision=revision,
        local_time=local_time,
        timezone=timezone,
        min_age_days=min_age_days,
        threshold_bytes=threshold_bytes,
        profile_id=profile_id,
        max_calls=max_calls,
        max_source_bytes=max_source_bytes,
        max_events=max_events,
        daily_call_limit=daily_call_limit,
    )
