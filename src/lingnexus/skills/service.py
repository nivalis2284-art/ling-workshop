"""Versioned procedural knowledge; activating a skill never grants capabilities."""

from __future__ import annotations

import hashlib
import json

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict, SourceInvalid
from lingnexus.common.types import SourceRef, new_id, utc_now


class Skills:
    def __init__(self, database, history):
        self.database, self.history = database, history

    def save(
        self,
        agent_id: str,
        name: str,
        description: str,
        instructions: str,
        sources: list[SourceRef],
        *,
        request_id: str,
    ) -> dict:
        if (
            not name.strip()
            or not description.strip()
            or not instructions.strip()
            or len(instructions) > 32000
        ):
            raise ValueError("技能名称、说明和方法无效")
        refs = [r.model_dump() for r in sources]
        digest = hashlib.sha256(
            json.dumps(
                [name, description, instructions, refs], ensure_ascii=False, sort_keys=True
            ).encode()
        ).hexdigest()
        with self.database.transaction() as db:
            old = db.execute(
                "SELECT id,fingerprint FROM skills WHERE agent_id=? AND request_id=?",
                (agent_id, request_id),
            ).fetchone()
            if old:
                if old["fingerprint"] != digest:
                    raise Conflict("技能请求编号冲突")
                return self.read(old["id"])
            self.history.validate_refs(agent_id, sources, "real")
            sid = new_id()
            db.execute(
                "INSERT INTO skills VALUES (?,?,?,?,?,?,?)",
                (sid, agent_id, name, description, 1, request_id, digest),
            )
            db.execute(
                "INSERT INTO skill_revisions VALUES (?,?,?,?,?,?)",
                (sid, 1, instructions, "candidate", json.dumps(refs), utc_now()),
            )
            return self.read(sid)

    def read(self, skill_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT s.id,s.agent_id,s.name,s.description,r.* FROM skills s JOIN skill_revisions r ON r.skill_id=s.id AND r.revision=s.current_revision WHERE s.id=?",
                (skill_id,),
            ).fetchone()
            if not row:
                raise NotFound("技能不存在")
            return dict(row) | {"sources": json.loads(row["sources"])}

    def list(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            return [
                self.read(r[0])
                for r in db.execute(
                    "SELECT id FROM skills WHERE agent_id=? ORDER BY rowid DESC", (agent_id,)
                ).fetchall()
            ]

    def available(self, agent_id: str) -> list[dict]:
        result = []
        for item in self.list(agent_id):
            if item["status"] != "active":
                continue
            result.append(item)
        return result

    def _revision(
        self, item: dict, instructions: str, status: str, sources: list[SourceRef]
    ) -> dict:
        with self.database.transaction() as db:
            rev = item["revision"] + 1
            db.execute(
                "INSERT INTO skill_revisions VALUES (?,?,?,?,?,?)",
                (
                    item["id"],
                    rev,
                    instructions,
                    status,
                    json.dumps([r.model_dump() for r in sources]),
                    utc_now(),
                ),
            )
            db.execute("UPDATE skills SET current_revision=? WHERE id=?", (rev, item["id"]))
            return self.read(item["id"])

    def decide(self, skill_id: str, action: str, *, expected_revision: int) -> dict:
        if action not in {"activate", "reject", "disable"}:
            raise ValueError("未知技能操作")
        with self.database.transaction():
            item = self.read(skill_id)
            if item["status"] == "stale":
                raise SourceInvalid("技能来源已经失效")
            if item["revision"] != expected_revision:
                raise RevisionConflict("技能版本已经改变")
            refs = [SourceRef(**r) for r in item["sources"]]
            return self._revision(
                item,
                item["instructions"],
                {"activate": "active", "reject": "rejected", "disable": "disabled"}[action],
                refs,
            )

    def revise(
        self,
        skill_id: str,
        instructions: str,
        sources: list[SourceRef],
        *,
        expected_revision: int,
        request_id: str,
    ) -> dict:
        if not instructions.strip() or len(instructions) > 32000:
            raise ValueError("技能方法无效")
        with self.database.transaction():
            item = self.read(skill_id)
            if item["revision"] != expected_revision:
                raise RevisionConflict("技能版本已经改变")
            self.history.validate_refs(item["agent_id"], sources, "real")
            return self._revision(item, instructions, "candidate", sources)

    def versions(self, skill_id: str) -> list[dict]:
        with self.database.transaction() as db:
            self.read(skill_id)
            return [
                dict(row) | {"sources": json.loads(row["sources"])}
                for row in db.execute(
                    "SELECT * FROM skill_revisions WHERE skill_id=? ORDER BY revision DESC",
                    (skill_id,),
                )
            ]


    def restore(self, skill_id: str, *, target_revision: int, expected_revision: int) -> dict:
        with self.database.transaction() as db:
            current = self.read(skill_id)
            if current["revision"] != expected_revision:
                raise RevisionConflict("技能版本已经改变")
            prior = db.execute("SELECT * FROM skill_revisions WHERE skill_id=? AND revision=?", (skill_id,target_revision)).fetchone()
            if prior is None:
                raise NotFound("指定技能版本不存在")
            refs = [SourceRef(**r) for r in json.loads(prior["sources"])]
            if prior["status"] == "stale":
                raise SourceInvalid("不能恢复已经失效的技能")
            return self._revision(current,prior["instructions"],prior["status"],refs)
