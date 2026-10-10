import hashlib
import json

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict
from lingnexus.common.types import SourceRef, new_id, utc_now


class DevelopmentService:
    def __init__(self, database, history):
        self.database, self.history = database, history

    def propose_goal(
        self, agent_id: str, title: str, sources: list[SourceRef], *, request_id: str
    ) -> dict:
        if not title.strip() or not request_id:
            raise ValueError("目标标题和请求编号不能为空")
        refs = sorted({(r.id, r.revision) for r in sources})
        digest = hashlib.sha256(json.dumps([title, refs], ensure_ascii=False).encode()).hexdigest()
        with self.database.transaction() as db:
            old = db.execute(
                "SELECT * FROM goals WHERE agent_id=? AND request_id=?", (agent_id, request_id)
            ).fetchone()
            if old:
                if old["fingerprint"] != digest:
                    raise Conflict("目标请求编号冲突")
                return self.read(old["id"])
            self.history.validate_refs(agent_id, sources)
            gid = new_id()
            db.execute(
                "INSERT INTO goals VALUES (?,?,?,?,?)", (gid, agent_id, request_id, digest, 1)
            )
            db.execute(
                "INSERT INTO goal_revisions VALUES (?,?,?,?,?,?,?)",
                (gid, 1, title, "proposed", "", "proposed", utc_now()),
            )
            db.executemany("INSERT INTO goal_sources VALUES (?,?,?)", [(gid, *ref) for ref in refs])
            return self.read(gid)

    def read(self, goal_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT g.id,g.agent_id,r.* FROM goals g JOIN goal_revisions r ON r.goal_id=g.id AND r.revision=g.current_revision WHERE g.id=?",
                (goal_id,),
            ).fetchone()
            if not row:
                raise NotFound("目标不存在")
            refs = [
                SourceRef(id=r[0], revision=r[1])
                for r in db.execute(
                    "SELECT source_id,source_revision FROM goal_sources WHERE goal_id=?", (goal_id,)
                )
            ]
            return dict(row) | {"sources": [r.model_dump() for r in refs]}

    def list(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            return [
                self.read(row[0])
                for row in db.execute(
                    "SELECT id FROM goals WHERE agent_id=?", (agent_id,)
                ).fetchall()
            ]

    def _write(self, value: dict, *, status: str, progress: str, reason: str) -> dict:
        with self.database.transaction() as db:
            revision = value["revision"] + 1
            db.execute(
                "INSERT INTO goal_revisions VALUES (?,?,?,?,?,?,?)",
                (value["id"], revision, value["title"], status, progress, reason, utc_now()),
            )
            db.execute("UPDATE goals SET current_revision=? WHERE id=?", (revision, value["id"]))
            return self.read(value["id"])

    def decide(self, goal_id: str, action: str, *, expected_revision: int) -> dict:
        transitions = {
            "activate": ({"proposed", "paused"}, "active"),
            "pause": ({"active"}, "paused"),
            "complete": ({"active"}, "completed"),
            "abandon": ({"proposed", "active", "paused", "needs_review"}, "abandoned"),
        }
        if action not in transitions:
            raise ValueError("未知目标操作")
        with self.database.transaction():
            value = self.read(goal_id)
            allowed, status = transitions[action]
            if value["revision"] != expected_revision or value["status"] not in allowed:
                raise RevisionConflict("目标状态或版本已经改变")
            return self._write(
                value, status=status, progress=value["progress"], reason="owner:" + action
            )

    def update_progress(self, goal_id: str, progress: str, *, expected_revision: int) -> dict:
        with self.database.transaction():
            value = self.read(goal_id)
            if value["revision"] != expected_revision or value["status"] != "active":
                raise RevisionConflict("目标未启用或已被修改")
            return self._write(value, status="active", progress=progress, reason="progress")

