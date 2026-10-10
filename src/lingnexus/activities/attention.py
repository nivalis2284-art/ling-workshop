"""Bounded attention observations and short, revision-checked state changes."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from lingnexus.common.errors import NotFound, PermissionDenied, SourceInvalid
from lingnexus.common.types import new_id, utc_now
from lingnexus.context.recall import candidates
from lingnexus.memory.maintenance import MemoryMaintenance
from lingnexus.tools.workspace import FileLimitExceeded

from .sources import ObservationError, fetch_web, read_file, web_target


class AttentionReview:
    def __init__(self, app, activity: dict):
        self.app, self.agent_id = app, activity["agent_id"]
        self.observations, self.refs, self.markers, self.selected = [], [], [], []
        with app.database.transaction():
            self.activity = app.activities.read(activity["id"])
            if (
                self.activity["agent_id"] != self.agent_id
                or self.activity["kind"] != "topic_review"
                or self.activity["status"] != "active"
                or self.activity["revision"] != activity.get("revision")
            ):
                raise PermissionDenied("Attention has changed or is inactive")
            self.permissions = app.permissions.current(self.agent_id)
            self.grants = app.activities.web_origins(self.agent_id)
            self.scopes = app.activities.scopes(self.agent_id)
            self.items = self._items()
            self.state = app.identity.current(self.agent_id)
            self.goals = app.development.list(self.agent_id)
            self.baselines = {s["id"]: self._baseline(s) for s in self.scopes}

    def _items(self) -> list[dict]:
        items, offset = [], 0
        while True:
            page = self.app.activities.list(self.agent_id, limit=100, offset=offset)
            items.extend(page)
            if len(page) < 100:
                return items
            offset += 100

    def _key(self, scope: dict) -> str:
        return f"attention:{self.activity['id']}:scope:{scope['id']}"

    def _baseline(self, scope: dict) -> dict:
        with self.app.database.transaction() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (self._key(scope),)
            ).fetchone()
            return json.loads(row[0]) if row else {}

    @staticmethod
    def _watch_revisions(items: list[dict]) -> list[tuple]:
        return [
            (a["id"], a["revision"], a["status"])
            for a in items
            if a["kind"] in {"file_watch", "web_watch"}
        ]

    def valid(self) -> bool:
        """Call after every I/O await and inside the final write transaction."""
        try:
            with self.app.database.transaction():
                current = self.app.activities.read(self.activity["id"])
                return (
                    current["status"] == "active"
                    and current["revision"] == self.activity["revision"]
                    and self.app.identity.current(self.agent_id).revision == self.state.revision
                    and self.app.permissions.current(self.agent_id) == self.permissions
                    and self.app.activities.web_origins(self.agent_id) == self.grants
                    and self.app.activities.scopes(self.agent_id) == self.scopes
                    and self._watch_revisions(self._items()) == self._watch_revisions(self.items)
                    and self.app.development.list(self.agent_id) == self.goals
                    and all(self._baseline(s) == self.baselines[s["id"]] for s in self.selected)
                )
        except (NotFound, PermissionDenied):
            return False

    def allowed(self, scope: dict) -> bool:
        if scope["status"] != "authorized":
            return False
        if any(
            a["scope_id"] == scope["id"] and a["status"] in {"paused", "cancelled"}
            for a in self.items
        ):
            return False
        if scope["kind"] == "file_watch":
            return any(
                Path(scope["target"]).is_relative_to(Path(root))
                for root in self.permissions["readable"]
            )
        target, origin = web_target(scope["target"])
        return target == scope["target"] and origin in self.grants["origins"]

    async def observe(self, client: httpx.AsyncClient) -> bool:
        scopes = [s for s in self.scopes if self.allowed(s)]
        scopes.sort(key=lambda s: self.baselines[s["id"]].get("observed_at", ""))
        self.selected = scopes[:2]
        for scope in self.selected:
            if not self.valid():
                return False
            try:
                if scope["kind"] == "web_watch":
                    observed = await fetch_web(client, scope["target"])
                else:
                    observed = read_file(self.app, scope)
            except (
                ObservationError,
                PermissionDenied,
                FileLimitExceeded,
                httpx.HTTPError,
                OSError,
                UnicodeError,
            ):
                if not self.valid():
                    return False
                continue
            if not self.valid():
                return False
            self.observations.append(
                observed
                | {
                    "scope_id": scope["id"],
                    "scope_revision": scope["revision"],
                    "kind": scope["kind"],
                    "target": scope["target"],
                    "previous_hash": self.baselines[scope["id"]].get("hash"),
                    "observed_at": utc_now(),
                }
            )
        return True

    @property
    def new_facts(self) -> list[dict]:
        return [o for o in self.observations if o["hash"] != o["previous_hash"]]

    def context(self, evidence: dict) -> dict:
        with self.app.database.transaction():
            query = self.activity["title"] + " " + " ".join(g["title"] for g in self.goals[:8])
            memories, self.refs, self.markers = candidates(
                self.app, self.agent_id, query[:1024]
            )
        bounded = [
            o
            | {
                "excerpt": o["excerpt"][:2000],
                "truncated": o["truncated"] or len(o["excerpt"]) > 2000,
            }
            for o in self.new_facts
        ]
        return {
            "attention": self.activity["title"],
            "goals": [{k: g[k] for k in ("title", "status", "progress")} for g in self.goals[:8]],
            "memories": memories,
            "evidence": evidence | {"observations": bounded, "untrusted_source": True},
            "authorized_sources": [
                {k: s[k] for k in ("kind", "target")} for s in self.scopes if self.allowed(s)
            ][:20],
            "active_activities": [
                {k: a[k] for k in ("kind", "title", "target")}
                for a in self.items
                if a["status"] == "active"
            ][:20],
        }

    def persist(self, db) -> None:
        for observed in self.observations:
            scope = next(s for s in self.selected if s["id"] == observed["scope_id"])
            if observed in self.new_facts:
                self.app.history.add(
                    self.agent_id,
                    json.dumps(
                        {"attention_id": self.activity["id"], **observed}, ensure_ascii=False
                    ),
                    kind="observation",
                    request_id="attention-observation:" + new_id(),
                )
            value = {"hash": observed["hash"], "observed_at": observed["observed_at"]}
            db.execute(
                "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._key(scope), json.dumps(value)),
            )

    def commit(self, followup=None) -> bool:
        with self.app.database.transaction() as db:
            if not self.valid():
                return False
            try:
                if self.refs:
                    self.app.history.validate_refs(self.agent_id, self.refs)
                MemoryMaintenance(self.app).validate_markers(self.agent_id, self.markers)
            except (SourceInvalid, NotFound):
                return False
            self.persist(db)
            if followup is not None:
                self._followup(followup)
            return True

    def _followup(self, followup) -> None:
        value = followup.model_dump()
        kind, target, title = value["kind"], value["target"], value["title"].strip()
        if not title or (kind == "topic_review" and target is not None):
            return
        if kind != "topic_review" and not any(
            s["kind"] == kind and s["target"] == target and self.allowed(s) for s in self.scopes
        ):
            return
        for item in self._items():
            if item["kind"] != kind or item["status"] == "completed":
                continue
            if (
                kind == "topic_review" and item["title"].strip().casefold() == title.casefold()
            ) or (kind != "topic_review" and item["target"] == target):
                return
        try:
            due = datetime.fromisoformat(self.activity["next_due_at"])
            due = max(due, datetime.fromisoformat(utc_now())) + timedelta(
                seconds=value["interval_seconds"]
            )
            self.app.activities.create(self.agent_id, **value, due_at=due, origin="model")
        except (ValueError, PermissionDenied):
            # A rejected low-risk follow-up never widens grants or loses the observation.
            return
