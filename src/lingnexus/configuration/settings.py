"""The single place that reads and writes the ``settings`` key/value table.

Every reader used to repeat the same four steps — query, ``json.loads``,
validate, fall back to a default. That is one behaviour written many times, so
any change to it (a new accepted type, a new failure mode) had to be repeated in
every copy, and the copies drifted. This module is the single source for it.

A stored value is *untrusted input*: it may be absent, hand-edited, or left over
from an older version. Every reader here therefore answers with a usable value,
never an exception — a corrupt setting must not take a whole feature down.
"""

from __future__ import annotations

import json


def read_json(database, key: str, default):
    """The stored JSON value, or ``default`` when missing or unparseable."""
    with database.transaction() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def read_int(database, key: str, default: int, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    """A stored integer within bounds, or ``default``. Bools are not integers."""
    value = read_json(database, key, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def read_choice(database, key: str, default, choices):
    """A stored value only when it is one of ``choices``; otherwise ``default``."""
    value = read_json(database, key, None)
    return value if value in choices else default


def write_json(database, key: str, value) -> None:
    """Store a value as JSON, replacing any previous one."""
    with database.transaction() as db:
        db.execute(
            "INSERT INTO settings VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def clear(database, key: str) -> None:
    """Remove a setting so the reader falls back to its default."""
    with database.transaction() as db:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
