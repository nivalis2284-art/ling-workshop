"""Bounded background command sessions with owner-scoped polling and cancellation.

Persistence is injected so this service does not depend on a particular schema:
``load`` returns saved records and ``save`` atomically replaces one record.
Running records found during startup become ``unknown`` and are never re-run.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

_FINAL = frozenset({"success", "failed", "timeout", "cancelled", "unknown"})
_PUBLIC_RESULT_FIELDS = frozenset({
    "status", "exit_code", "output_truncated", "timed_out", "cleanup_confirmed", "error",
})


class CommandSessions:
    """Own and observe non-interactive command runners.

    ``runner(cancel_event, emit_output)`` runs on a daemon worker thread and
    returns an ExecutionResult-like dataclass or mapping. Output cursors count
    Unicode code points in the combined, bounded output stream.
    """

    def __init__(self, database=None, data_dir=None, max_sessions: int = 2,
                 total_timeout: float = 900, *, max_output_bytes: int = 262_144,
                 load: Callable[[], Iterable[dict]] | None = None,
                 save: Callable[[dict], None] | None = None):
        if type(max_sessions) is not int or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")
        if type(total_timeout) not in (int, float) or total_timeout <= 0:
            raise ValueError("total_timeout must be positive")
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")
        if (load is None) != (save is None):
            raise ValueError("load and save callbacks must be provided together")
        self.database = database
        self.data_dir = data_dir
        self.max_sessions = max_sessions
        self.total_timeout = float(total_timeout)
        self.max_output_bytes = max_output_bytes
        self._load = load
        self._save = save
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._persistence_failures: set[str] = set()
        self._closed = False
        self._restore()

    def _restore(self) -> None:
        if self._load is None:
            return
        for saved in self._load() or ():
            if not isinstance(saved, dict) or not isinstance(saved.get("session_id"), str):
                continue
            record = dict(saved)
            output = record.get("output", "")
            if not isinstance(output, str):
                output = ""
            record["output"] = _bounded_text(output, self.max_output_bytes)
            record["output_bytes"] = len(record["output"].encode("utf-8", errors="replace"))
            record["output_truncated"] = bool(record.get("output_truncated")) or (
                record["output"] != output
            )
            if record.get("status") in {"running", "cancelling", "queued"}:
                record["status"] = "unknown"
                record["cancel_reason"] = None
                record["error"] = "process_state_after_restart"
                record["result"] = {"status": "unknown", "error": "process_state_after_restart"}
                self._save_record(record)
            self._records[record["session_id"]] = record

    def observation_guard(self):
        """Take lifecycle snapshots before database locks, matching worker order."""
        return self._lock

    def start(self, owner_id: str, run_id: str, argv: Sequence[str], runner, *, context=None) -> dict:
        """Persist then dispatch a runner; argv is stored privately, never polled."""
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id is required")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("argv must be a nonempty argument list")
        if any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValueError("argv items must be strings without NUL")
        if not callable(runner):
            raise TypeError("runner must be callable")
        try:
            inspect.signature(runner).bind(threading.Event(), lambda _text: None)
        except (TypeError, ValueError):
            raise ValueError("runner must accept (cancel_event, emit_output)")

        now = time.time()
        session_id = uuid4().hex
        record = {
            "session_id": session_id,
            "owner_id": owner_id,
            "run_id": run_id,
            "argv": list(argv),
            "created_at": _utc_iso(now),
            "deadline_at": _utc_iso(now + self.total_timeout),
            "status": "running",
            "output": "",
            "output_bytes": 0,
            "output_truncated": False,
            "cancel_reason": None,
            "error": None,
            "result": None,
            "context": dict(context or {}),
        }
        event = threading.Event()
        with self._lock:
            if self._closed:
                raise RuntimeError("command sessions are closed")
            active = sum(
                rec["run_id"] == run_id and rec["owner_id"] == owner_id
                and rec["status"] in {"running", "cancelling"}
                for rec in self._records.values()
            )
            if active >= self.max_sessions:
                raise RuntimeError("run_session_limit_reached")
            self._save_record(record)
            self._records[session_id] = record
            self._events[session_id] = event
            timer = threading.Timer(self.total_timeout, self._request_cancel,
                                    args=(session_id, "deadline"))
            timer.daemon = True
            self._timers[session_id] = timer
            worker = threading.Thread(target=self._run, args=(session_id, runner),
                                      name=f"command-session-{session_id[:8]}", daemon=True)
            self._workers[session_id] = worker
            timer.start()
            worker.start()
        return self._public(record)

    def poll(self, session_id: str, owner_id: str, after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise ValueError("after must be a nonnegative character offset")
        with self._lock:
            record = self._owned(session_id, owner_id)
            output = record["output"]
            if after > len(output):
                raise ValueError("after is beyond the current output cursor")
            result = self._public(record)
            result.update({"output": output[after:], "cursor": len(output)})
            return result

    def cancel(self, session_id: str, owner_id: str) -> dict:
        with self._lock:
            record = self._owned(session_id, owner_id)
            if record["status"] in _FINAL:
                return self._public(record)
            self._set_cancel_locked(record, "owner")
            return self._public(record)

    def details(self, session_id: str, owner_id: str) -> dict:
        """Private adapter access; API callers receive poll(), never this record."""
        from copy import deepcopy

        with self._lock:
            return deepcopy(self._owned(session_id, owner_id))

    def for_run(self, owner_id: str, run_id: str) -> list[dict]:
        """Return safe lifecycle metadata for one exact owner/run pair."""
        with self._lock:
            return [
                {"session_id": rec["session_id"], "status": rec["status"],
                 "cancel_reason": rec.get("cancel_reason"), "error": rec.get("error")}
                for rec in self._records.values()
                if rec.get("owner_id") == owner_id and rec.get("run_id") == run_id
            ]

    def for_kind(self, kind: str) -> list[dict]:
        """Return safe lifecycle metadata for sessions with a trusted context kind."""
        with self._lock:
            return [
                {"session_id": rec["session_id"], "status": rec["status"],
                 "run_id": rec.get("run_id"), "agent_id": (rec.get("context") or {}).get("agent_id"),
                 "cancel_reason": rec.get("cancel_reason"), "error": rec.get("error")}
                for rec in self._records.values()
                if (rec.get("context") or {}).get("kind") == kind
            ]

    def cancel_run(self, owner_id: str, run_id: str, reason: str = "owner") -> list[dict]:
        """Request cancellation for every active command in an exact run."""
        with self._lock:
            for rec in self._records.values():
                if (rec.get("owner_id") == owner_id and rec.get("run_id") == run_id
                        and rec["status"] in {"running", "cancelling"}):
                    self._set_cancel_locked(rec, reason)
            return self.for_run(owner_id, run_id)

    async def wait_run(self, owner_id: str, run_id: str, timeout: float = 5.0) -> list[dict]:
        """Wait boundedly for run sessions to report terminal cleanup evidence."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            records = self.for_run(owner_id, run_id)
            if not any(rec["status"] in {"running", "cancelling"} for rec in records):
                return records
            if time.monotonic() >= deadline:
                return records
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _request_cancel(self, session_id: str, reason: str) -> None:
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record["status"] in _FINAL:
                return
            self._set_cancel_locked(record, reason)

    def _set_cancel_locked(self, record: dict, reason: str) -> None:
        if record["status"] == "running":
            record["status"] = "cancelling"
            record["cancel_reason"] = reason
            event = self._events.get(record["session_id"])
            if event is not None:
                event.set()
            timer = self._timers.get(record["session_id"])
            if timer is not None and reason != "deadline":
                timer.cancel()
            self._persist_update_locked(record)

    def _run(self, session_id: str, runner) -> None:
        event = self._events[session_id]

        def emit_output(text: str) -> None:
            if not isinstance(text, str) or not text:
                return
            with self._lock:
                record = self._records[session_id]
                remaining = self.max_output_bytes - record["output_bytes"]
                if remaining <= 0:
                    record["output_truncated"] = True
                else:
                    kept = []
                    used = 0
                    for char in text:
                        size = len(char.encode("utf-8", errors="replace"))
                        if used + size > remaining:
                            break
                        kept.append(char)
                        used += size
                    record["output"] += "".join(kept)
                    record["output_bytes"] += used
                    if len(kept) != len(text):
                        record["output_truncated"] = True
                self._persist_update_locked(record)

        try:
            raw_result = runner(event, emit_output)
            result = _result_dict(raw_result)
            with self._lock:
                record = self._records[session_id]
                # Support simple runners that return captured output rather than
                # streaming it. Prefer streamed chunks to prevent duplicate text.
                if not record["output"]:
                    for channel in (result.pop("stdout", ""), result.pop("stderr", "")):
                        if isinstance(channel, str) and channel:
                            self._append_locked(record, channel)
                reason = record.get("cancel_reason")
                cleanup_failed = (
                    result.get("cleanup_confirmed") is False
                    or result.get("error") in {
                        "process_cleanup_unconfirmed", "container_cleanup_unconfirmed",
                    }
                )
                explicit_cancel = (
                    (bool(result.get("cancelled")) or result.get("status") == "cancelled")
                    and not cleanup_failed
                )
                timed_out = (
                    (bool(result.get("timed_out")) or result.get("status") == "timeout")
                    and not cleanup_failed
                )
                if reason == "deadline":
                    if timed_out or explicit_cancel:
                        status = "timeout"
                    else:
                        status = "unknown"
                        result["error"] = "timeout_cleanup_unconfirmed"
                elif reason == "owner":
                    if explicit_cancel:
                        status = "cancelled"
                    elif timed_out:
                        status = "timeout"
                    else:
                        status = "unknown"
                        result["error"] = "cancellation_unconfirmed"
                else:
                    status = result.get("status", "unknown")
                    if status not in _FINAL:
                        status = "unknown"
                if session_id in self._persistence_failures:
                    status = "unknown"
                    result["error"] = "session_persistence_failed"
                result.pop("cancelled", None)
                result.pop("stdout", None)
                result.pop("stderr", None)
                result["status"] = status
                if record["output_truncated"]:
                    result["output_truncated"] = True
                record["status"] = status
                record["error"] = result.get("error")
                record["result"] = result
                self._finish_locked(record)
        except BaseException as exc:
            with self._lock:
                record = self._records[session_id]
                record["status"] = "unknown"
                record["error"] = "runner_failed"
                record["result"] = {"status": "unknown", "error": "runner_failed"}
                self._finish_locked(record)
            # Do not leak exception messages (they may include paths or arguments).
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

    def _append_locked(self, record: dict, text: str) -> None:
        remaining = self.max_output_bytes - record["output_bytes"]
        kept = []
        used = 0
        for char in text:
            size = len(char.encode("utf-8", errors="replace"))
            if used + size > remaining:
                break
            kept.append(char)
            used += size
        record["output"] += "".join(kept)
        record["output_bytes"] += used
        if len(kept) != len(text):
            record["output_truncated"] = True

    def _finish_locked(self, record: dict) -> None:
        session_id = record["session_id"]
        timer = self._timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()
        try:
            self._save_record(record)
        except Exception:  # noqa: BLE001 -- persist uncertainty without leaking private errors
            self._persistence_failures.add(session_id)
            record["status"] = "unknown"
            record["error"] = "session_persistence_failed"
            record["result"] = {"status": "unknown", "error": "session_persistence_failed"}
            try:
                self._save_record(record)
            except Exception:  # noqa: BLE001 -- in-memory unknown state remains authoritative
                self._persistence_failures.add(session_id)
        self._events.pop(session_id, None)
        self._workers.pop(session_id, None)

    def _owned(self, session_id: str, owner_id: str) -> dict:
        record = self._records.get(session_id)
        if record is None or record.get("owner_id") != owner_id:
            raise PermissionError("command session not found")
        return record

    def _save_record(self, record: dict) -> None:
        if self._save is not None:
            self._save(dict(record))

    def _persist_update_locked(self, record: dict) -> None:
        try:
            self._save_record(record)
        except Exception:  # noqa: BLE001 -- a failed persistence callback requires cancellation
            session_id = record["session_id"]
            self._persistence_failures.add(session_id)
            event = self._events.get(session_id)
            if event is not None:
                event.set()

    def _public(self, record: dict) -> dict:
        result = record.get("result")
        if isinstance(result, dict):
            result = {key: value for key, value in result.items()
                      if key in _PUBLIC_RESULT_FIELDS}
        return {
            "session_id": record["session_id"],
            "status": record["status"],
            "output_truncated": bool(record.get("output_truncated")),
            "error": record.get("error"),
            "result": result,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for record in self._records.values():
                if record["status"] == "running":
                    self._set_cancel_locked(record, "shutdown")

    async def aclose(self) -> None:
        self.close()
        workers = list(self._workers.values())
        await asyncio.gather(*(asyncio.to_thread(worker.join, 3) for worker in workers))


def _result_dict(result) -> dict:
    if isinstance(result, dict):
        return dict(result)
    if hasattr(result, "__dataclass_fields__"):
        return {name: getattr(result, name) for name in result.__dataclass_fields__}
    return {"status": "unknown", "error": "invalid_runner_result"}


def _bounded_text(value: str, max_bytes: int = 262_144) -> str:
    kept = []
    used = 0
    for char in value:
        size = len(char.encode("utf-8", errors="replace"))
        if used + size > max_bytes:
            break
        kept.append(char)
        used += size
    return "".join(kept)


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()
