"""Transactions for task checkpoints, event cursors and uncertain effect fences."""

import json

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.common.types import new_id, utc_now

TASK_TABLES = ("tasks", "task_checkpoints", "task_steps", "task_events", "task_reviews")
TERMINAL = {"completed", "needs_review", "cancelled"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=lambda v: v.model_dump())


class TaskStore:
    def __init__(self, database):
        self.database = database

    def get(self, tid):
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                raise NotFound("Task not found.")
            result = dict(row)
            result.pop("fingerprint")
            result["verified"] = bool(result["verified"])
            state = json.loads(
                db.execute("SELECT state FROM task_checkpoints WHERE task_id=?", (tid,)).fetchone()[
                    0
                ]
            )
            result["environment"] = state.get("environment", "python")
            for key in ("dependency_candidate", "dependency_statuses", "owner_instruction"):
                if key in state:
                    result[key] = state[key]
            if state.get("waiting_owner"):
                result.update(state["waiting_owner"])
            return result

    def lookup(self, agent_id, request_id, fingerprint):
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT id,fingerprint FROM tasks WHERE agent_id=? AND request_id=?",
                (agent_id, request_id),
            ).fetchone()
            if row and row["fingerprint"] != fingerprint:
                raise Conflict("Task request ID already has different parameters.")
            return row["id"] if row else None

    def insert(self, tid, spec, fingerprint, profile_id, image, limit, state):
        with self.database.transaction() as db:
            old = self.lookup(spec["agent_id"], spec["request_id"], fingerprint)
            if old:
                return old
            now = utc_now()
            db.execute(
                "INSERT INTO tasks(id,agent_id,objective,request_id,fingerprint,workspace,"
                "profile_id,docker_image,status,step_limit,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    spec["agent_id"],
                    spec["objective"],
                    spec["request_id"],
                    fingerprint,
                    spec.get("workspace"),
                    profile_id,
                    image,
                    "queued",
                    limit,
                    now,
                    now,
                ),
            )
            db.execute("INSERT INTO task_checkpoints VALUES(?,?)", (tid, encode(state)))
            self.event(tid, "status", {"status": "queued"})
            return tid

    def event(self, tid, kind, data):
        with self.database.transaction() as db:
            cursor = db.execute(
                "INSERT INTO task_events(task_id,type,data,created_at) VALUES(?,?,?,?)",
                (tid, kind, encode(data), utc_now()),
            )
            return cursor.lastrowid

    def events(self, tid, after=0, limit=200):
        self.get(tid)
        with self.database.transaction() as db:
            return [
                {**dict(r), "data": json.loads(r["data"])}
                for r in db.execute(
                    "SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                    (tid, after, max(1, min(limit, 1000))),
                )
            ]

    def load(self, tid):
        self.get(tid)
        with self.database.transaction() as db:
            return json.loads(
                db.execute(
                    "SELECT state FROM task_checkpoints WHERE task_id=?",
                    (tid,),
                ).fetchone()[0]
            )

    def save(self, tid, state, *, model_step=False):
        with self.database.transaction() as db:
            db.execute("UPDATE task_checkpoints SET state=? WHERE task_id=?", (encode(state), tid))
            db.execute(
                "UPDATE tasks SET step_count=step_count+?,updated_at=? WHERE id=?",
                (int(model_step), utc_now(), tid),
            )

    def status(self, tid, status, error=None, *, final=None, verified=False):
        with self.database.transaction() as db:
            old = self.get(tid)
            if old["status"] in TERMINAL:
                return old
            db.execute(
                "UPDATE tasks SET status=?,error_code=?,final=COALESCE(?,final),"
                "verified=?,updated_at=? WHERE id=?",
                (status, error, final, int(verified), utc_now(), tid),
            )
            self.event(tid, "status", {"status": status, "error_code": error})
            return self.get(tid)

    def claim(self, tid):
        with self.database.transaction() as db:
            changed = db.execute(
                "UPDATE tasks SET status='running',error_code=NULL,updated_at=? "
                "WHERE id=? AND status='queued'",
                (utc_now(), tid),
            ).rowcount
            if changed:
                self.event(tid, "status", {"status": "running"})
            return bool(changed)

    def pending(self):
        with self.database.transaction() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,status FROM tasks WHERE status IN ('queued','running')"
                )
            ]

    def resume(self, tid, additional_steps):
        with self.database.transaction() as db:
            row = self.get(tid)
            if row["status"] in TERMINAL:
                raise Conflict("A terminal task cannot resume.")
            if row["status"] == "running":
                return
            if (
                row["step_count"] >= row["step_limit"]
                and not additional_steps
                and not self.load(tid)["pending"]
            ):
                raise Conflict("Task step limit requires an owner extension.")
            db.execute(
                "UPDATE tasks SET step_limit=step_limit+?,status='queued',error_code=NULL,"
                "updated_at=? WHERE id=?",
                (additional_steps, utc_now(), tid),
            )
            self.event(tid, "status", {"status": "queued"})

    def step(self, tid, number, call):
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM task_steps WHERE task_id=? AND model_step=? AND call_id=?",
                (tid, number, call["id"]),
            ).fetchone()
            if row:
                if row["name"] != call["name"] or row["arguments"] != encode(call["arguments"]):
                    raise Conflict("Tool ID has inconsistent parameters.")
                return {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}
            sid = new_id()
            purpose = call["arguments"].get("purpose", self.get(tid)["objective"])
            db.execute(
                "INSERT INTO task_steps VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    tid,
                    number,
                    call["id"],
                    call["name"],
                    encode(call["arguments"]),
                    purpose,
                    "prepared",
                    None,
                    None,
                    utc_now(),
                ),
            )
            return self.step(tid, number, call)

    def executing(self, sid, effect_id=None):
        with self.database.transaction() as db:
            db.execute(
                "UPDATE task_steps SET status='executing',effect_id=? WHERE id=?",
                (effect_id or sid, sid),
            )

    def result(self, sid, result, status):
        with self.database.transaction() as db:
            db.execute(
                "UPDATE task_steps SET status=?,result=? WHERE id=?", (status, encode(result), sid)
            )

    def effect(self, sid, effect_id):
        with self.database.transaction() as db:
            db.execute("UPDATE task_steps SET effect_id=? WHERE id=?", (effect_id, sid))

    def review(self, tid, manifest):
        rid = new_id()
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO task_reviews VALUES(?,?,?,?,?,?)",
                (rid, tid, encode(manifest), "reviewed", None, utc_now()),
            )
        return rid

    def get_review(self, tid, rid):
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM task_reviews WHERE id=? AND task_id=?", (rid, tid)
            ).fetchone()
            if not row:
                raise NotFound("Change review not found.")
            return {
                **dict(row),
                "manifest": json.loads(row["manifest"]),
                "result": json.loads(row["result"]) if row["result"] else None,
            }

    def review_status(self, rid, status, result=None):
        with self.database.transaction() as db:
            db.execute(
                "UPDATE task_reviews SET status=?,result=? WHERE id=?",
                (status, encode(result) if result else None, rid),
            )
