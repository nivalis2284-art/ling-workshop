"""Durable schedules and an atomic evidence/notification outbox.

No worker is started here. A parent may await tick from any scheduler. The
investigator must use the parent's normal model context and treat excerpts and
event content as untrusted evidence, never as instructions or authorization.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

import httpx

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied, RevisionConflict
from lingnexus.common.types import new_id
from lingnexus.tools.workspace import FileLimitExceeded

from .sources import (
    ObservationError,
    fetch_web,
    file_target,
    read_file,
    validate_file,
    web_origin,
    web_target,
)

Investigator = Callable[[dict, dict], Awaitable[dict | None]]
KINDS = {"reminder", "file_watch", "web_watch", "topic_review"}
WATCHES = {"file_watch", "web_watch"}


class _Unset(Enum):
    TOKEN = 0


_UNSET = _Unset.TOKEN


def _time(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scheduling requires a timezone-aware datetime")
    return value.astimezone(UTC)


def _stamp(value: datetime | str | None = None) -> str:
    return _time(value).isoformat(timespec="microseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {label}")
    return value.strip()


def _origin(value: str) -> None:
    if value not in {"owner", "model"}:
        raise ValueError("Origin must be owner or model")


def _revision(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("Expected revision must be a positive integer")


def _interval(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or not 60 <= value <= 315_360_000):
        raise ValueError("Recurring intervals must be integer seconds from 60 to 315360000")
    return value


class ActivityService:
    """Owner/model origin is supplied by trusted parent code, never model arguments.

    Model tool adapters must force origin='model'. The HTTP client remains owned
    by the caller. Investigate is async and returns None (pending), or a dict with
    changed: bool, summary: str, and optional notification: str. It receives the
    public activity plus bounded completion evidence. Only the parent knows how
    to invoke its main model prompt; this module does not fabricate that decision.
    """

    def __init__(
        self,
        application,
        client: httpx.AsyncClient | None = None,
        *,
        investigate: Investigator | None = None,
    ):
        self.application = application
        self.database = application.database
        self.client = client
        self.investigate = investigate

    def _agent(self, agent_id: str) -> None:
        self.application.permissions.current(agent_id)

    def _raw(self, db, activity_id: str) -> dict:
        row = db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if row is None:
            raise NotFound("Activity does not exist")
        return dict(row)

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            k: v
            for k, v in row.items()
            if k not in {"claim_token", "claim_until", "authorization", "events"}
        } | {
            "busy": row["claim_token"] is not None,
        }

    def read(self, activity_id: str) -> dict:
        with self.database.transaction() as db:
            return self._public(self._raw(db, activity_id))

    def list(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        self._page(limit, offset)
        if status is not None and status not in {"active", "paused", "cancelled", "completed"}:
            raise ValueError("Invalid activity status")
        with self.database.transaction() as db:
            self._agent(agent_id)
            sql, params = "SELECT * FROM activities WHERE agent_id=?", [agent_id]
            if status is not None:
                sql += " AND status=?"
                params.append(status)
            rows = db.execute(
                sql + " ORDER BY created_at,rowid LIMIT ? OFFSET ?", (*params, limit, offset)
            ).fetchall()
            return [self._public(dict(r)) for r in rows]

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid page bounds")

    def web_origins(self, agent_id: str) -> dict:
        with self.database.transaction() as db:
            self._agent(agent_id)
            row = db.execute(
                "SELECT * FROM activity_web_grants WHERE agent_id=?", (agent_id,)
            ).fetchone()
            return {
                "agent_id": agent_id,
                "revision": row["revision"] if row else 0,
                "origins": json.loads(row["origins"]) if row else [],
            }

    def configure_web_origins(
        self,
        agent_id: str,
        origins: list[str],
        *,
        expected_revision: int,
        origin: str = "owner",
    ) -> dict:
        _origin(origin)
        if origin != "owner":
            raise PermissionDenied("Only the owner can authorize outbound origins")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid grant revision")
        if not isinstance(origins, list) or len(origins) > 100:
            raise ValueError("At most 100 exact origins may be configured")
        normalized = sorted({web_origin(o) for o in origins})
        with self.database.transaction() as db:
            old = self.web_origins(agent_id)
            if old["revision"] != expected_revision:
                raise RevisionConflict("Web authorization has changed")
            now = _stamp()
            db.execute(
                "INSERT INTO activity_web_grants VALUES (?,?,?,?) ON CONFLICT(agent_id) "
                "DO UPDATE SET revision=excluded.revision,origins=excluded.origins,"
                "updated_at=excluded.updated_at",
                (agent_id, expected_revision + 1, _json(normalized), now),
            )
            for row in db.execute(
                "SELECT id,web_origin FROM activity_scopes WHERE agent_id=? "
                "AND kind='web_watch' AND status='authorized'",
                (agent_id,),
            ).fetchall():
                if row["web_origin"] not in normalized:
                    self._revoke(db, row["id"], now)
            return self.web_origins(agent_id)

    def scopes(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            self._agent(agent_id)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM activity_scopes WHERE agent_id=? ORDER BY created_at,rowid",
                    (agent_id,),
                )
            ]

    def revoke_scope(
        self,
        scope_id: str,
        *,
        expected_revision: int,
        origin: str = "owner",
    ) -> dict:
        _origin(origin)
        _revision(expected_revision)
        if origin != "owner":
            raise PermissionDenied("Only the owner can revoke watch authorization")
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM activity_scopes WHERE id=?", (scope_id,)).fetchone()
            if row is None:
                raise NotFound("Watch scope does not exist")
            if row["revision"] != expected_revision:
                raise RevisionConflict("Watch authorization has changed")
            self._revoke(db, scope_id, _stamp())
            return dict(
                db.execute("SELECT * FROM activity_scopes WHERE id=?", (scope_id,)).fetchone()
            )

    @staticmethod
    def _revoke(db, scope_id: str, now: str) -> None:
        db.execute(
            "UPDATE activity_scopes SET status='revoked',revision=revision+1,"
            "updated_at=? WHERE id=?",
            (now, scope_id),
        )
        db.execute(
            "UPDATE activities SET status='paused',revision=revision+1,"
            "last_outcome='permission_revoked',last_error='permission_revoked',"
            "claim_token=NULL,claim_until=NULL,updated_at=? "
            "WHERE scope_id=? AND status IN ('active','paused')",
            (now, scope_id),
        )

    def _permission(self, agent_id: str, kind: str, target: str) -> int:
        if kind == "file_watch":
            state = self.application.permissions.current(agent_id)
            if not any(Path(target).is_relative_to(Path(root)) for root in state["readable"]):
                raise PermissionDenied("File is outside current readable directory grants")
        else:
            state = self.web_origins(agent_id)
            if web_target(target)[1] not in state["origins"]:
                raise PermissionDenied("Outbound origin has not been authorized")
        return state["revision"]

    def _scope(self, db, agent_id: str, kind: str, target: str, origin: str) -> str:
        self._permission(agent_id, kind, target)
        row = db.execute(
            "SELECT * FROM activity_scopes WHERE agent_id=? AND kind=? AND target=?",
            (agent_id, kind, target),
        ).fetchone()
        if origin == "model" and (row is None or row["status"] != "authorized"):
            raise PermissionDenied("Model creation requires an exact owner-authorized watch target")
        if origin == "owner" and kind == "file_watch":
            validate_file(target)
        if row is not None and row["status"] == "authorized":
            return row["id"]
        sid, now = row["id"] if row else new_id(), _stamp()
        db.execute(
            "INSERT INTO activity_scopes VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id,kind,target) DO UPDATE SET status='authorized',"
            "revision=activity_scopes.revision+1,updated_at=excluded.updated_at",
            (
                sid,
                agent_id,
                kind,
                target,
                web_target(target)[1] if kind == "web_watch" else None,
                1,
                "authorized",
                now,
                now,
            ),
        )
        return sid

    def _authorization(self, db, item: dict) -> tuple[int, int] | None:
        if item["kind"] not in WATCHES:
            return None
        row = db.execute("SELECT * FROM activity_scopes WHERE id=?", (item["scope_id"],)).fetchone()
        if (
            row is None
            or row["status"] != "authorized"
            or row["agent_id"] != item["agent_id"]
            or row["kind"] != item["kind"]
            or row["target"] != item["target"]
        ):
            raise PermissionDenied("Watch authorization has been revoked")
        return row["revision"], self._permission(item["agent_id"], item["kind"], item["target"])

    def create(
        self,
        agent_id: str,
        *,
        kind: str,
        title: str,
        target: str | None = None,
        due_at: datetime | str | None = None,
        interval_seconds: int | None = None,
        origin: str = "owner",
    ) -> dict:
        _origin(origin)
        title = _text(title, 200, "activity title")
        if kind not in KINDS:
            raise ValueError("Invalid activity kind")
        interval_seconds = _interval(interval_seconds)
        if kind != "reminder" and interval_seconds is None:
            interval_seconds = 60
        due = _stamp(due_at)
        if kind in WATCHES:
            if target is None:
                raise ValueError("Watch activities require an exact target")
            target = file_target(target) if kind == "file_watch" else web_target(target)[0]
        elif target is not None:
            raise ValueError("This activity kind does not accept a watch target")
        with self.database.transaction() as db:
            self._agent(agent_id)
            scope_id = self._scope(db, agent_id, kind, target, origin) if target else None
            aid, now = new_id(), _stamp()
            db.execute(
                "INSERT INTO activities(id,agent_id,kind,title,target,origin,scope_id,revision,"
                "status,next_due_at,interval_seconds,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,1,'active',?,?,?,?)",
                (
                    aid,
                    agent_id,
                    kind,
                    title,
                    target,
                    origin,
                    scope_id,
                    due,
                    interval_seconds,
                    now,
                    now,
                ),
            )
            return self.read(aid)

    def update(
        self,
        activity_id: str,
        *,
        expected_revision: int,
        title: str | _Unset = _UNSET,
        target: str | None | _Unset = _UNSET,
        due_at: datetime | str | None | _Unset = _UNSET,
        interval_seconds: int | None | _Unset = _UNSET,
        status: str | _Unset = _UNSET,
        origin: str = "owner",
    ) -> dict:
        _origin(origin)
        _revision(expected_revision)
        with self.database.transaction() as db:
            item = self._raw(db, activity_id)
            if item["revision"] != expected_revision:
                raise RevisionConflict("Activity has changed")
            if item["status"] in {"cancelled", "completed"}:
                raise Conflict("Terminal activities cannot be modified")
            if title is not _UNSET:
                item["title"] = _text(title, 200, "activity title")
                if (
                    item["kind"] == "topic_review"
                    and item["title"] != self._raw(db, activity_id)["title"]
                ):
                    item["last_hash"] = None
            if due_at is not _UNSET:
                item["next_due_at"] = _stamp(due_at)
            if interval_seconds is not _UNSET:
                item["interval_seconds"] = _interval(interval_seconds)
                if item["kind"] != "reminder" and interval_seconds is None:
                    raise ValueError("Watch and review activities must recur")
            if target is not _UNSET:
                if item["kind"] not in WATCHES or target is None:
                    raise ValueError("Only watch activities accept exact targets")
                normalized = (
                    file_target(target) if item["kind"] == "file_watch" else web_target(target)[0]
                )
                item["scope_id"] = self._scope(
                    db,
                    item["agent_id"],
                    item["kind"],
                    normalized,
                    origin,
                )
                if normalized != item["target"]:
                    item["last_hash"] = None
                item["target"] = normalized
            if status is not _UNSET:
                if status not in {"active", "paused", "cancelled"}:
                    raise ValueError("Invalid activity transition")
                if origin == "model" and status == "active" and item["status"] == "paused":
                    raise PermissionDenied("Only the owner can resume a paused activity")
                item["status"] = status
            if item["status"] == "active":
                self._authorization(db, item)
            db.execute(
                "UPDATE activities SET title=?,target=?,scope_id=?,next_due_at=?,"
                "interval_seconds=?,status=?,last_hash=?,revision=revision+1,"
                "claim_token=NULL,claim_until=NULL,updated_at=? WHERE id=?",
                (
                    item["title"],
                    item["target"],
                    item["scope_id"],
                    item["next_due_at"],
                    item["interval_seconds"],
                    item["status"],
                    item["last_hash"],
                    _stamp(),
                    activity_id,
                ),
            )
            return self.read(activity_id)

    def pause(self, activity_id: str, *, expected_revision: int, origin: str = "owner") -> dict:
        return self.update(
            activity_id, expected_revision=expected_revision, status="paused", origin=origin
        )

    def cancel(self, activity_id: str, *, expected_revision: int, origin: str = "owner") -> dict:
        return self.update(
            activity_id, expected_revision=expected_revision, status="cancelled", origin=origin
        )

    def evidence(self, activity_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        self._page(limit, offset)
        with self.database.transaction() as db:
            self._raw(db, activity_id)
            return [
                dict(r)
                | {
                    "payload": json.loads(r["payload"]),
                    "source": {"id": r["source_id"], "revision": r["source_revision"]},
                }
                for r in db.execute(
                    "SELECT * FROM activity_evidence WHERE activity_id=? "
                    "ORDER BY rowid LIMIT ? OFFSET ?",
                    (activity_id, limit, offset),
                )
            ]

    def notifications(self, agent_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        self._page(limit, offset)
        with self.database.transaction() as db:
            self._agent(agent_id)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM activity_notifications WHERE agent_id=? "
                    "ORDER BY rowid LIMIT ? OFFSET ?",
                    (agent_id, limit, offset),
                )
            ]

    @staticmethod
    def _event(row) -> dict:
        return {
            k: row[k]
            for k in ("id", "seq", "agent_id", "dedupe_key", "kind", "content", "recorded_at")
        } | {
            "source": {"id": row["source_id"], "revision": row["source_revision"]},
        }

    def record_event(
        self,
        agent_id: str,
        *,
        dedupe_key: str,
        kind: str,
        content: str,
        now: datetime | str | None = None,
    ) -> dict:
        """Trusted parent hook for actual tool/task completion, not a model tool."""
        key = _text(dedupe_key, 200, "event dedupe key")
        content = _text(content, 8192, "event content")
        if kind not in {"tool_completed", "task_completed"}:
            raise ValueError("Only completion events are supported")
        fingerprint = hashlib.sha256(_json([kind, content]).encode()).hexdigest()
        stamp = _stamp(now)
        with self.database.transaction() as db:
            self._agent(agent_id)
            old = db.execute(
                "SELECT * FROM activity_events WHERE agent_id=? AND dedupe_key=?",
                (agent_id, key),
            ).fetchone()
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("Event dedupe key already has different content")
                return self._event(old)
            eid = new_id()
            ref = self.application.history.add(
                agent_id,
                _json({"kind": kind, "content": content, "event_id": eid}),
                kind="tool",
                request_id="activity-event:" + eid,
            )
            cursor = db.execute(
                "INSERT INTO activity_events(id,agent_id,dedupe_key,kind,content,fingerprint,"
                "source_id,source_revision,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, agent_id, key, kind, content, fingerprint, ref.id, ref.revision, stamp),
            ).lastrowid
            for activity in db.execute(
                "SELECT id,last_checked_at FROM activities WHERE agent_id=? "
                "AND kind='topic_review' AND status IN ('active','paused')",
                (agent_id,),
            ).fetchall():
                earliest = stamp
                if activity["last_checked_at"] is not None:
                    earliest = max(
                        stamp, _stamp(_time(activity["last_checked_at"]) + timedelta(seconds=60))
                    )
                db.execute(
                    "UPDATE activities SET next_due_at=MIN(next_due_at,?),trigger_seq=? WHERE id=?",
                    (earliest, cursor, activity["id"]),
                )
            return self._event(
                db.execute("SELECT * FROM activity_events WHERE id=?", (eid,)).fetchone()
            )

    def _claim(self, now: datetime) -> dict | None:
        with self.database.transaction() as db:
            wall = _stamp()
            row = db.execute(
                "SELECT * FROM activities WHERE status='active' AND next_due_at<=? "
                "AND (claim_token IS NULL OR claim_until<=?) ORDER BY next_due_at,rowid LIMIT 1",
                (_stamp(now), wall),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            try:
                item["authorization"] = self._authorization(db, item)
            except PermissionDenied:
                self._permission_pause(db, item["id"], wall)
                return {"id": item["id"], "blocked": True}
            token = new_id()
            db.execute(
                "UPDATE activities SET claim_token=?,claim_until=? WHERE id=?",
                (token, _stamp(_time() + timedelta(seconds=90)), item["id"]),
            )
            item["claim_token"] = token
            item["events"] = (
                [
                    self._event(r)
                    for r in db.execute(
                        "SELECT * FROM activity_events WHERE agent_id=? AND seq>? ORDER BY seq LIMIT 50",
                        (item["agent_id"], item["event_cursor"]),
                    )
                ]
                if item["kind"] == "topic_review"
                else []
            )
            return item

    @staticmethod
    def _permission_pause(db, activity_id: str, now: str) -> None:
        db.execute(
            "UPDATE activities SET status='paused',revision=revision+1,claim_token=NULL,"
            "claim_until=NULL,last_outcome='permission_revoked',last_error='permission_revoked',"
            "updated_at=? WHERE id=?",
            (now, activity_id),
        )

    async def tick(self, now: datetime | str | None = None) -> dict | None:
        """Return None when idle, otherwise activity_id, outcome and error_code.

        Evidence/notification IDs are returned when committed. At most one due
        active activity is processed; no transaction spans an await. Claims have a
        90-second lease, and I/O/investigation has a 30-second total deadline.
        """
        clock = _time(now)
        item = self._claim(clock)
        if item is None:
            return None
        if item.get("blocked"):
            return {
                "activity_id": item["id"],
                "outcome": "permission_revoked",
                "error_code": "permission_revoked",
            }
        try:
            async with asyncio.timeout(30):
                payload = await self._observe(item, clock)
        except asyncio.CancelledError:
            self._release(item)
            raise
        except PermissionDenied:
            return self._finish(item, clock, None, "permission_revoked")
        except ObservationError as exc:
            return self._finish(item, clock, None, exc.code)
        except FileLimitExceeded:
            return self._finish(item, clock, None, "content_too_large")
        except (TimeoutError, httpx.TimeoutException):
            return self._finish(item, clock, None, "timeout")
        except httpx.HTTPError:
            return self._finish(item, clock, None, "http_failed")
        except UnicodeError:
            return self._finish(item, clock, None, "invalid_encoding")
        except OSError:
            return self._finish(item, clock, None, "file_unavailable")
        except Exception:  # noqa: BLE001 -- persist only finite codes at the untrusted I/O boundary
            # Callback failures may contain secrets or enormous upstream responses.
            return self._finish(
                item,
                clock,
                None,
                "investigation_failed" if item["kind"] == "topic_review" else "observation_failed",
            )
        return self._finish(item, clock, payload, None)

    def _release(self, item: dict) -> None:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE activities SET claim_token=NULL,claim_until=NULL "
                "WHERE id=? AND claim_token=?",
                (item["id"], item["claim_token"]),
            )

    async def _observe(self, item: dict, now: datetime) -> dict | None:
        if item["kind"] == "reminder":
            return {"title": item["title"], "due_at": item["next_due_at"]}
        if item["kind"] == "file_watch":
            return read_file(self.application, item)
        if item["kind"] == "web_watch":
            if self.client is not None:
                return await fetch_web(self.client, item["target"])
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await fetch_web(client, item["target"])
        if self.investigate is None:
            return None
        evidence = {
            "kind": "topic_review",
            "title": item["title"],
            "observed_at": _stamp(now),
            "events": item["events"],
            "untrusted_source": True,
        }
        decision = await self.investigate(self._public(item), evidence)
        if decision is None:
            return None
        if not isinstance(decision, dict) or set(decision) - {"changed", "summary", "notification"}:
            raise ObservationError("invalid_investigation")
        if type(decision.get("changed")) is not bool:
            raise ObservationError("invalid_investigation")
        try:
            summary = _text(decision.get("summary"), 4096, "investigation summary")
            message = decision.get("notification")
            if message is not None:
                message = _text(message, 2000, "investigation notification")
        except ValueError:
            raise ObservationError("invalid_investigation") from None
        return {
            "changed": decision["changed"],
            "summary": summary,
            "notification": message,
            "events": item["events"],
            "hash": hashlib.sha256(_json([summary, message]).encode()).hexdigest(),
        }

    def _finish(
        self,
        item: dict,
        now: datetime,
        payload: dict | None,
        error: str | None,
    ) -> dict:
        with self.database.transaction() as db:
            current = self._raw(db, item["id"])
            result = {"activity_id": item["id"], "outcome": "discarded", "error_code": None}
            if (
                current["status"] != "active"
                or current["revision"] != item["revision"]
                or current["claim_token"] != item["claim_token"]
                or current["claim_until"] <= _stamp()
                or current["next_due_at"] != item["next_due_at"]
            ):
                self._release(item)
                return result
            try:
                authorization = self._authorization(db, current)
            except PermissionDenied:
                self._permission_pause(db, item["id"], _stamp())
                return result
            if authorization != item["authorization"]:
                self._release(item)
                return result
            if error == "permission_revoked":
                self._permission_pause(db, item["id"], _stamp())
                return result | {"outcome": "permission_revoked", "error_code": error}
            outcome, message, store = "unchanged", None, False
            new_hash, event_cursor = item["last_hash"], item["event_cursor"]
            if error is not None:
                outcome = "error"
            elif payload is None:
                outcome = "pending_investigation"
            elif item["kind"] == "reminder":
                outcome, message, store = "reminded", item["title"], True
            elif item["kind"] in WATCHES:
                new_hash = payload["hash"]
                if item["last_hash"] is None:
                    outcome, store = "baseline", True
                elif new_hash != item["last_hash"]:
                    outcome, store = "changed", True
                    message = f"{item['title']}：关注的资料出现了变化，可以查看新内容。"
            else:
                new_hash = payload["hash"]
                if item["events"]:
                    event_cursor = item["events"][-1]["seq"]
                if payload["changed"] and new_hash != item["last_hash"]:
                    outcome, store = "changed", True
                    message = payload["notification"] or payload["summary"]
            result |= {"outcome": outcome, "error_code": error}
            if store and payload is not None:
                result |= self._store(db, item, now, payload, outcome, message)
            status = "active"
            if item["interval_seconds"] is None and error is None:
                status, next_due = "completed", item["next_due_at"]
            else:
                interval = item["interval_seconds"] or 60
                due = _time(item["next_due_at"])
                # Skip all missed slots in one operation, retaining the original phase.
                slots = max(1, int((now - due).total_seconds() // interval) + 1)
                next_due = _stamp(due + timedelta(seconds=slots * interval))
                if error is not None or payload is None:
                    next_due = max(next_due, _stamp(now + timedelta(seconds=60)))
                if item["kind"] == "topic_review" and current["trigger_seq"] > event_cursor:
                    # Bounded event batches wait at least a minute; they never flood ticks.
                    next_due = min(next_due, _stamp(now + timedelta(seconds=60)))
            db.execute(
                "UPDATE activities SET status=?,next_due_at=?,last_hash=?,last_outcome=?,"
                "last_error=?,event_cursor=?,last_checked_at=?,revision=revision+1,"
                "claim_token=NULL,claim_until=NULL,"
                "updated_at=? WHERE id=?",
                (
                    status,
                    next_due,
                    new_hash,
                    outcome,
                    error,
                    event_cursor,
                    _stamp(now),
                    _stamp(),
                    item["id"],
                ),
            )
            return result

    def _store(self, db, item: dict, now: datetime, payload: dict, outcome: str, message) -> dict:
        stamp = _stamp(now)
        key = f"{item['revision']}:{item['next_due_at']}"
        old = db.execute(
            "SELECT id FROM activity_evidence WHERE activity_id=? AND dedupe_key=?",
            (item["id"], key),
        ).fetchone()
        if old is not None:
            return {"evidence_id": old["id"]}
        eid = new_id()
        content = {
            "activity_id": item["id"],
            "title": item["title"],
            "kind": item["kind"],
            "outcome": outcome,
            "observed_at": stamp,
            **payload,
        }
        ref = self.application.history.add(
            item["agent_id"],
            _json(content),
            kind="observation",
            request_id="activity-evidence:" + eid,
        )
        db.execute(
            "INSERT INTO activity_evidence VALUES (?,?,?,?,?,?,?,?)",
            (eid, item["id"], key, item["kind"], _json(content), ref.id, ref.revision, stamp),
        )
        result = {"evidence_id": eid}
        if message is not None:
            nid = new_id()
            db.execute(
                "INSERT INTO activity_notifications VALUES (?,?,?,?,?,?)",
                (nid, item["id"], item["agent_id"], eid, message, stamp),
            )
            result["notification_id"] = nid
        return result
