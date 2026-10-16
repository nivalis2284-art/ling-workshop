"""Safe semantic records for work events: filtered text, chunked and paged.

This module is storage, not policy. The runtime decides which text is safe to
keep (filtering, secret guards and preview truncation all happen before a call
lands here); the store only guarantees honest attribution, idempotent capture,
immutable bodies and byte-accurate UTF-8 paging.

Attribution: a record's agent and conversation are read from the real
``chat_runs`` row of ``run_id``. The authorization snapshot prefers the run's
checkpoint grants, or ``metadata['scope_grants']`` when the trusted runtime
supplies the exact admission snapshot, and only falls back to the current
revision when neither exists — a later withdrawal must never re-label an old
capture as newly authorized.

Body text is stored as 64 KiB byte chunks, so one page request reads at most
two chunks and never materializes a multi-megabyte result in memory.
"""

from __future__ import annotations

import hashlib
import json

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.common.types import new_id, utc_now

CHUNK_BYTES = 64 * 1024
MAX_PAGE_BYTES = 16384
DEFAULT_PAGE_BYTES = 8192
MAX_METADATA_BYTES = 64 * 1024
MAX_KIND_CHARS = 64
MAX_ID_CHARS = 200

COVERAGE_RETAINED = "retained"


class EventRecordStore:
    """Durable ledger of safe, owner-readable text for work-event sources."""

    def __init__(self, app):
        self.app = app
        self.database = app.database

    # --- capture ---------------------------------------------------------

    def record(
        self,
        run_id: str,
        kind: str,
        text: str,
        *,
        step: int = 0,
        call_id: str = "",
        attempt: int = 0,
        metadata: dict | None = None,
    ) -> dict:
        """Store one already-filtered piece of text for a real run.

        ``kind`` is the runtime's own label for the capture point (a tool call's
        arguments, a tool result, mid-run guidance, aggregated visible text or
        thinking, a step reply). Identical (run, kind, step, call_id, attempt)
        replays are idempotent and return the stored record; different content at
        the same position raises ``Conflict`` instead of overwriting history.
        The body is never truncated: it is split into 64 KiB byte chunks.
        """
        run_id = _identifier(run_id, "run_id")
        kind = _kind(kind)
        if not isinstance(text, str):
            raise TypeError("正文必须是文本。")
        step = _counter(step, "step")
        attempt = _counter(attempt, "attempt")
        call_id = _call_id(call_id)
        encoded_metadata = _metadata_json(metadata)
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        with self.database.transaction() as db:
            run = db.execute(
                "SELECT agent_id,conversation_id FROM chat_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise NotFound("运行不存在，不能登记安全记录。")
            agent_id = run["agent_id"]
            # Legacy runs may predate conversation binding; an empty scope stays
            # honest instead of inventing membership.
            conversation_id = run["conversation_id"] or ""
            old = db.execute(
                "SELECT * FROM work_event_records "
                "WHERE run_id=? AND kind=? AND step=? AND call_id=? AND attempt=?",
                (run_id, kind, step, call_id, attempt),
            ).fetchone()
            if old is not None:
                if old["sha256"] != digest or (old["metadata"] or None) != encoded_metadata:
                    raise Conflict("同一运行位置已有不同的安全记录，拒绝覆盖。")
                return _record_row(old) | {"reused": True, "coverage": COVERAGE_RETAINED}
            grants = json.dumps(self._grants(run_id, agent_id, metadata), ensure_ascii=False)
            record_id = new_id()
            created_at = utc_now()
            db.execute(
                "INSERT INTO work_event_records(id,run_id,agent_id,conversation_id,kind,"
                "step,call_id,attempt,metadata,grants,sha256,byte_count,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    run_id,
                    agent_id,
                    conversation_id,
                    kind,
                    step,
                    call_id,
                    attempt,
                    encoded_metadata,
                    grants,
                    digest,
                    len(data),
                    created_at,
                ),
            )
            if data:
                db.executemany(
                    "INSERT INTO work_event_chunks(record_id,ordinal,body) VALUES(?,?,?)",
                    [
                        (record_id, ordinal, data[index : index + CHUNK_BYTES])
                        for ordinal, index in enumerate(range(0, len(data), CHUNK_BYTES))
                    ],
                )
            return {
                "id": record_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "kind": kind,
                "step": step,
                "call_id": call_id,
                "attempt": attempt,
                "metadata": metadata,
                "grants": json.loads(grants),
                "sha256": digest,
                "byte_count": len(data),
                "created_at": created_at,
                "coverage": COVERAGE_RETAINED,
                "reused": False,
            }

    # --- metadata views --------------------------------------------------

    def for_runs(self, run_ids) -> list[dict]:
        """Every record of these runs as metadata only; bodies stay unread.

        The event directory uses this to hint at what a run captured without
        loading any text; a page of text is requested per record through
        ``read``. Unknown run ids contribute nothing.
        """
        wanted = _run_ids(run_ids)
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT * FROM work_event_records WHERE run_id IN (" + marks + ") "
                "ORDER BY rowid",
                tuple(wanted),
            ).fetchall()
        return [_record_row(row) | {"coverage": COVERAGE_RETAINED} for row in rows]

    # --- paged reading ---------------------------------------------------

    def read(self, record_id: str, *, offset: int = 0, max_bytes: int = DEFAULT_PAGE_BYTES) -> dict:
        """One UTF-8-safe page of a record's body, by byte offset.

        ``offset`` is always a position produced by a previous page's
        ``next_offset`` (or 0). At most two 64 KiB chunks are read for one page,
        the returned page never exceeds ``max_bytes``, and it never splits a
        character: ``next_offset`` is the exact byte position where the next page
        resumes. ``next_offset`` is ``None`` once the body is exhausted.
        """
        record_id = _identifier(record_id, "record_id")
        offset = _counter(offset, "offset")
        if type(max_bytes) is not int or not 4 <= max_bytes <= MAX_PAGE_BYTES:
            raise ValueError(f"单页字节数须在 4 到 {MAX_PAGE_BYTES} 之间。")
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM work_event_records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise NotFound("安全记录不存在。")
            byte_count = row["byte_count"]
            if offset >= byte_count:
                return _read_row(row) | {
                    "text": "",
                    "offset": offset,
                    "next_offset": None,
                    "page_bytes": 0,
                }
            first = offset // CHUNK_BYTES
            last = min(byte_count - 1, offset + max_bytes - 1) // CHUNK_BYTES
            buffer = self._page_bytes(db, record_id, first, last)
        base = first * CHUNK_BYTES
        data = buffer[offset - base : min(byte_count, offset + max_bytes) - base]
        text = _decode_page(data, offset)
        used = len(text.encode("utf-8"))
        return _read_row(row) | {
            "text": text,
            "offset": offset,
            "next_offset": None if offset + used >= byte_count else offset + used,
            "page_bytes": used,
        }

    def _page_bytes(self, db, record_id: str, first: int, last: int) -> bytes:
        """Concatenated bodies of the chunks covering one byte range."""
        rows = db.execute(
            "SELECT body FROM work_event_chunks WHERE record_id=? AND ordinal BETWEEN ? AND ? "
            "ORDER BY ordinal",
            (record_id, first, last),
        ).fetchall()
        return b"".join(row["body"] for row in rows)

    # --- authorization snapshot -----------------------------------------

    def _grants(self, run_id: str, agent_id: str, metadata: dict | None) -> dict:
        """The authorization snapshot this capture was admitted under.

        Order: the runtime's explicit ``scope_grants``, then the run's checkpoint
        grants, then the current revision. Browser origin grants are included for
        the same reason as the file grants.
        """
        raw, captured_from = None, "current"
        if isinstance(metadata, dict) and isinstance(metadata.get("scope_grants"), dict):
            raw, captured_from = metadata["scope_grants"], "runtime"
        if raw is None:
            with self.database.transaction() as db:
                stored = db.execute(
                    "SELECT state FROM chat_run_checkpoints WHERE run_id=?", (run_id,)
                ).fetchone()
            if stored is not None:
                try:
                    state = json.loads(stored["state"])
                except ValueError:
                    state = None
                grants = state.get("grants") if isinstance(state, dict) else None
                if isinstance(grants, dict):
                    raw, captured_from = grants, "checkpoint"
        if raw is None:
            raw = self.app.permissions.current(agent_id)
        if isinstance(raw, dict) and ("permissions" in raw or "browser" in raw):
            snapshot = {"permissions": raw.get("permissions"), "browser": raw.get("browser")}
        else:
            snapshot = {"permissions": raw, "browser": None}
        browser = getattr(self.app, "browser", None)
        if snapshot["browser"] is None and browser is not None:
            snapshot["browser"] = browser.current_origins(agent_id)
        return snapshot | {"captured_from": captured_from}


# --- row shaping and input checks ----------------------------------------


def _record_row(row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "agent_id": row["agent_id"],
        "conversation_id": row["conversation_id"],
        "kind": row["kind"],
        "step": row["step"],
        "call_id": row["call_id"],
        "attempt": row["attempt"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
        "grants": json.loads(row["grants"]),
        "sha256": row["sha256"],
        "byte_count": row["byte_count"],
        "created_at": row["created_at"],
    }


def _read_row(row) -> dict:
    return _record_row(row) | {
        "coverage": COVERAGE_RETAINED,
        "location": {
            "run_id": row["run_id"],
            "kind": row["kind"],
            "step": row["step"],
            "call_id": row["call_id"],
            "attempt": row["attempt"],
            "chunk_bytes": CHUNK_BYTES,
        },
    }


def _decode_page(data: bytes, offset: int) -> str:
    """Decode one page, refusing a split character instead of losing bytes."""
    if not data:
        return ""
    if data[0] & 0xC0 == 0x80:
        raise ValueError("offset 未落在 UTF-8 字符边界。")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason != "unexpected end of data" or error.start == 0:
            raise ValueError("记录片段不是有效 UTF-8。") from None
        return data[: error.start].decode("utf-8")


def _identifier(value, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS:
        raise ValueError(f"{name} 无效。")
    return value


def _kind(value) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_KIND_CHARS
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("记录类型无效。")
    return value.strip()


def _call_id(value) -> str:
    if not isinstance(value, str) or len(value) > MAX_ID_CHARS:
        raise ValueError("call_id 无效。")
    if any(ord(char) < 32 for char in value):
        raise ValueError("call_id 无效。")
    return value


def _counter(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} 必须是非负整数。")
    return value


def _metadata_json(metadata) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise TypeError("metadata 必须是对象或省略。")
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata 超过大小上限。")
    return encoded


def _run_ids(run_ids) -> list[str]:
    if isinstance(run_ids, str) or not hasattr(run_ids, "__iter__"):
        raise ValueError("run_ids 必须是字符串序列。")
    wanted: list[str] = []
    for value in run_ids:
        value = _identifier(value, "run_id")
        if value not in wanted:
            wanted.append(value)
    return wanted
