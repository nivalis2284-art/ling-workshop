"""Immutable plans with durable pending/running/result fences; no task writes."""

import json

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.common.types import new_id, utc_now

from .driver import digest, encode

DEPENDENCY_TABLES = ("dependency_plans",)


class DependencyStore:
    def __init__(self, database):
        self.database = database

    def get(self, plan_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM dependency_plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                raise NotFound("Dependency plan not found.")
            value = dict(row)
            value.pop("fingerprint")
            value["plan"] = json.loads(value["plan"])
            value["result"] = json.loads(value["result"]) if value["result"] else None
            return value

    def lookup(self, task_id: str, request_id: str, fingerprint: str) -> dict | None:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT id,fingerprint FROM dependency_plans WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            ).fetchone()
            if row and row["fingerprint"] != fingerprint:
                raise Conflict("Dependency request ID already has different parameters.")
            return self.get(row["id"]) if row else None

    def list(self, task_id: str) -> list[dict]:
        with self.database.transaction() as db:
            return [self.get(r[0]) for r in db.execute(
                "SELECT id FROM dependency_plans WHERE task_id=? ORDER BY created_at,id", (task_id,)
            )]

    def assert_unfenced(self, task_id: str, *, excluding: str = "") -> None:
        with self.database.transaction() as db:
            if db.execute(
                "SELECT 1 FROM dependency_plans WHERE task_id=? AND id!=? "
                "AND status IN ('running','unknown') LIMIT 1", (task_id, excluding),
            ).fetchone():
                raise Conflict("Task dependency execution is active or unknown; owner reconciliation is required.")

    def insert(self, task_id: str, request_id: str, fingerprint: str, plan: dict) -> dict:
        with self.database.transaction() as db:
            old = self.lookup(task_id, request_id, fingerprint)
            if old:
                return old
            plan_id, now = new_id(), utc_now()
            db.execute(
                "INSERT INTO dependency_plans(id,task_id,request_id,fingerprint,plan,intent_hash,"
                "status,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                (plan_id, task_id, request_id, fingerprint, encode(plan), digest(plan), now, now),
            )
            return self.get(plan_id)

    def result(self, plan_id: str, status: str, *, decision: str | None = None,
               result: dict | None = None, error: str | None = None) -> dict:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE dependency_plans SET status=?,decision=COALESCE(?,decision),"
                "result=?,error_code=?,updated_at=? WHERE id=?",
                (status, decision, encode(result) if result is not None else None,
                 error, utc_now(), plan_id),
            )
            return self.get(plan_id)

    def recover(self, active: set[tuple[str, str]], database_key: str) -> None:
        with self.database.transaction() as db:
            for row in db.execute("SELECT id FROM dependency_plans WHERE status='running'").fetchall():
                if (database_key, row["id"]) not in active:
                    self.result(row["id"], "unknown", error="process_interrupted")
