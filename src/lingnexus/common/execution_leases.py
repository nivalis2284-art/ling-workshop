"""Durable resource fences shared by Chat and Task; unknown never expires.

A conservative database-wide filesystem resource also covers parent/child path
aliases and legacy Task application. Waiting does not hold a database transaction.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from threading import RLock

from lingnexus.common.errors import Conflict
from lingnexus.common.types import new_id, utc_now

_ACTIVE = set()
_LOCK = RLock()


class ExecutionLeases:
    def __init__(self, app):
        self.app = app
        self.database = app.database

    def acquire(self, owner, resource="filesystem"):
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM execution_resource_leases WHERE resource=?", (resource,)).fetchone()
            if row:
                if row["status"] == "unknown":
                    raise Conflict("工作区仍有未知副作用，须人工核对。")
                return None
            token = new_id()
            db.execute("INSERT INTO execution_resource_leases(resource,owner,token,status,created_at) VALUES(?,?,?,'held',?)", (resource, owner, token, utc_now()))
            with _LOCK:
                _ACTIVE.add(token)
            return token

    def finish(self, owner, token, *, unknown=False):
        with _LOCK:
            _ACTIVE.discard(token)
        with self.database.transaction() as db:
            if unknown:
                db.execute("UPDATE execution_resource_leases SET status='unknown' WHERE owner=? AND token=?", (owner, token))
            else:
                db.execute("DELETE FROM execution_resource_leases WHERE owner=? AND token=? AND status='held'", (owner, token))

    def reconcile_sessions(self):
        """Only confirmed terminal command cleanup releases a retained fence."""
        with self.database.transaction() as db:
            rows = db.execute("SELECT * FROM execution_resource_leases WHERE session_id IS NOT NULL").fetchall()
        for row in rows:
            if row["status"] == "unknown":
                continue
            sessions = self.app.sandbox.run_sessions(row["agent_id"], row["run_id"], row["session_kind"])
            exact = next((s for s in sessions if s["session_id"] == row["session_id"]), None)
            if exact and exact["status"] not in {"running", "cancelling", "unknown"}:
                self.finish(row["owner"], row["token"])
            elif exact is None or exact["status"] == "unknown":
                self.finish(row["owner"], row["token"], unknown=True)

    def recover_orphans(self):
        """Process-start observation only; never expire and reexecute effects."""
        with self.database.transaction() as db, _LOCK:
            rows = db.execute("SELECT token FROM execution_resource_leases WHERE status='held' AND session_id IS NULL").fetchall()
            for row in rows:
                if row["token"] not in _ACTIVE:
                    db.execute("UPDATE execution_resource_leases SET status='unknown' WHERE token=?", (row["token"],))
        self.reconcile_sessions()

    def pending(self):
        with self.database.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM execution_resource_leases")]

    def reconcile(self, token, *, observation):
        """Owner-only manual observation; preserves the old effect's unknown state.

        A mounts this method behind owner authentication. It releases admission
        for NEW operations only, never replays or marks an old effect successful.
        """
        if type(observation) is not str or not observation.strip() or len(observation.encode()) > 4096:
            raise ValueError("请记录对未知副作用与工作区的人工核对。")
        self.reconcile_sessions()
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM execution_resource_leases WHERE token=?", (token,)).fetchone()
            if not row or row["status"] != "unknown":
                raise Conflict("仅可核对未知资源围栏。")
            with _LOCK:
                if token in _ACTIVE:
                    raise Conflict("操作仍在执行。")
            if row["session_id"]:
                session = next((s for s in self.app.sandbox.run_sessions(row["agent_id"], row["run_id"], row["session_kind"]) if s["session_id"] == row["session_id"]), None)
                if session and session["status"] in {"running", "cancelling"}:
                    raise Conflict("命令仍在执行。")
            db.execute("INSERT INTO execution_lease_reviews VALUES(?,?,?,?)", (token, row["owner"], observation, utc_now()))
            db.execute("DELETE FROM execution_resource_leases WHERE token=? AND status='unknown'", (token,))

    @asynccontextmanager
    async def guard(self, owner):
        token = None
        while token is None:
            self.reconcile_sessions()
            token = self.acquire(owner)
            if token is None:
                await asyncio.sleep(0.025)
        outcome = {"unknown": True, "retain": False, "session_id": None}
        try:
            yield outcome
        finally:
            if outcome["unknown"]:
                self.finish(owner, token, unknown=True)
            elif outcome["retain"] and outcome.get("session_id"):
                kind, agent, run = owner.split(":", 2)
                with self.database.transaction() as db:
                    db.execute("UPDATE execution_resource_leases SET session_id=?,session_kind=?,agent_id=?,run_id=? WHERE token=? AND owner=?",
                               (outcome["session_id"], kind, agent, run, token, owner))
                with _LOCK:
                    _ACTIVE.discard(token)
            elif outcome["retain"]:
                self.finish(owner, token, unknown=True)
            else:
                self.finish(owner, token)

    async def wait_available(self):
        """Legacy Actions atomically claims its own token through the SQL trigger."""
        while True:
            self.reconcile_sessions()
            rows = self.pending()
            if any(row["status"] == "unknown" for row in rows):
                raise Conflict("工作区仍有未知副作用，须人工核对。")
            if not rows:
                return
            await asyncio.sleep(0.025)


def result_fence(result):
    """Native result data determines whether a lease can actually be released."""
    return {"unknown": result.get("status") == "unknown" or result.get("cleanup_confirmed") is False,
            "retain": result.get("status") in {"running", "cancelling"},
            "session_id": (result.get("execution") or {}).get("session_id")}
