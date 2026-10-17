"""Daily IANA calendar arithmetic, independent of the host clock and locale."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_DATE_YEARS = 1
MAX_DATE_YEARS = 100


class CalendarError(ValueError):
    pass


def parse_local_time(value: str) -> time:
    try:
        hour, minute = value.split(":")
        if len(hour) != 2 or len(minute) != 2:
            raise ValueError
        moment = time(int(hour), int(minute))
    except (AttributeError, TypeError, ValueError):
        raise CalendarError("local_time must use HH:MM") from None
    return moment


def zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise CalendarError("timezone must be an IANA zone name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CalendarError("timezone is unavailable") from None


def as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("timestamp is invalid") from None
    if value.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return as_utc(value).isoformat()


def occurrence(day: date, requested: time, zoneinfo: ZoneInfo) -> dict:
    """Resolve one wall time: gap goes forward, fold uses the first instant."""
    requested_dt = datetime.combine(day, requested, tzinfo=zoneinfo)
    roundtrip = requested_dt.astimezone(UTC).astimezone(zoneinfo)
    valid = roundtrip.replace(tzinfo=None) == requested_dt.replace(tzinfo=None)
    first = requested_dt.replace(fold=0)
    second = requested_dt.replace(fold=1)
    ambiguous = first.utcoffset() != second.utcoffset()
    if valid:
        chosen = first if ambiguous else requested_dt
        return {
            "requested_local": requested_dt.isoformat(),
            "actual_local": chosen.isoformat(),
            "due_utc": iso(chosen),
            "fold": int(chosen.fold),
            "resolution": "fold_zero_first_occurrence" if ambiguous else "ordinary",
        }
    start = datetime.combine(day, time.min, tzinfo=zoneinfo).astimezone(UTC)
    limit = start + timedelta(hours=36)
    cursor = start
    while cursor <= limit:
        actual = cursor.astimezone(zoneinfo)
        if actual.date() == day and actual.timetz().replace(tzinfo=None) >= requested:
            return {
                "requested_local": requested_dt.isoformat(),
                "actual_local": actual.isoformat(),
                "due_utc": cursor.isoformat(),
                "fold": 0,
                "resolution": "gap_forward_to_first_valid_minute",
            }
        cursor += timedelta(minutes=1)
    raise CalendarError("daily occurrence could not be resolved")


def next_after(now: datetime, zoneinfo: ZoneInfo, local_time: time) -> dict:
    local = as_utc(now).astimezone(zoneinfo)
    for offset in (0, 1, 2):
        value = occurrence(local.date() + timedelta(days=offset), local_time, zoneinfo)
        if as_utc(value["due_utc"]) > as_utc(now):
            return value
    raise CalendarError("next occurrence was not found")


def latest_due(now: datetime, zoneinfo: ZoneInfo, local_time: time) -> dict:
    local = as_utc(now).astimezone(zoneinfo)
    for offset in (0, -1, -2):
        value = occurrence(local.date() + timedelta(days=offset), local_time, zoneinfo)
        if as_utc(value["due_utc"]) <= as_utc(now):
            return value
    raise CalendarError("latest due occurrence was not found")


def utc_day(moment: datetime) -> str:
    return as_utc(moment).date().isoformat()
