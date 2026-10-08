import hashlib
import json

from lingnexus.common.errors import Conflict, NotFound, SourceInvalid
from lingnexus.common.types import SourceRef, new_id, utc_now


class Knowledge:
    def __init__(self, database, history):
        self.database, self.history = database, history

    def save(
        self,
        agent_id: str,
        statement: str,
        sources: list[SourceRef],
        *,
        kind: str,
        request_id: str,
        scope: str = "real",
    ) -> str:
        if (
            kind not in {"fact", "preference", "experience", "hypothesis"}
            or not statement.strip()
            or not request_id
        ):
            raise ValueError("知识条目的类型或内容无效")
        refs = sorted({(r.id, r.revision) for r in sources})
        digest = hashlib.sha256(
            json.dumps([statement, kind, scope, refs], ensure_ascii=False).encode()
        ).hexdigest()
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM knowledge WHERE agent_id=? AND request_id=?", (agent_id, request_id)
            ).fetchone()
            if row:
                if row["fingerprint"] != digest:
                    raise Conflict("知识请求编号冲突")
                return row["id"]
            self.history.validate_refs(agent_id, sources, scope)
            kid = new_id()
            db.execute(
                "INSERT INTO knowledge(id,agent_id,statement,kind,scope,status,request_id,fingerprint,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (kid, agent_id, statement, kind, scope, "active", request_id, digest, utc_now()),
            )
            db.executemany(
                "INSERT INTO knowledge_sources VALUES (?,?,?)", [(kid, *ref) for ref in refs]
            )
            return kid

    def read(self, knowledge_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone()
            if row is None:
                raise NotFound("知识条目不存在")
            if row["status"] != "active":
                raise SourceInvalid("知识条目的依据已失效")
            refs = [
                SourceRef(id=r[0], revision=r[1])
                for r in db.execute(
                    "SELECT source_id,source_revision FROM knowledge_sources WHERE knowledge_id=?",
                    (knowledge_id,),
                )
            ]
            self.history.validate_refs(row["agent_id"], refs, row["scope"])
            return dict(row) | {"sources": [r.model_dump() for r in refs]}

    def search(
        self, agent_id: str, query: str, *, scope: str = "real", limit: int = 20, offset: int = 0
    ) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("无效分页")
        literal = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT id FROM knowledge WHERE agent_id=? AND scope=? AND status='active' AND statement LIKE ? ESCAPE '!' ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (agent_id, scope, f"%{literal}%", limit, offset),
            ).fetchall()
            return [self.read(row[0]) for row in rows]

    def invalidate(self, ref: SourceRef) -> None:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE knowledge SET status='invalidated' WHERE id IN (SELECT knowledge_id FROM knowledge_sources WHERE source_id=? AND source_revision=?)",
                (ref.id, ref.revision),
            )
