import hashlib
import json

from lingnexus.common.errors import Conflict, NotFound, SourceInvalid
from lingnexus.common.types import SourceRef, new_id, utc_now


class Episodes:
    def __init__(self, database, history):
        self.database, self.history = database, history

    def save(
        self,
        agent_id: str,
        title: str,
        summary: str,
        sources: list[SourceRef],
        *,
        request_id: str,
        scope: str = "real",
    ) -> str:
        if not title.strip() or not summary.strip() or not request_id:
            raise ValueError("标题、摘要和请求编号不能为空")
        refs = sorted({(r.id, r.revision) for r in sources})
        fingerprint = hashlib.sha256(
            json.dumps([title, summary, scope, refs], ensure_ascii=False).encode()
        ).hexdigest()
        with self.database.transaction() as db:
            existing = db.execute(
                "SELECT id,fingerprint FROM episodes WHERE agent_id=? AND request_id=?",
                (agent_id, request_id),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise Conflict("事件请求编号已用于不同内容")
                return existing["id"]
            self.history.validate_refs(agent_id, sources, scope)
            eid = new_id()
            db.execute(
                "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    agent_id,
                    title,
                    summary,
                    scope,
                    1,
                    "active",
                    request_id,
                    fingerprint,
                    utc_now(),
                ),
            )
            db.executemany(
                "INSERT INTO episode_sources VALUES (?,?,?)", [(eid, *ref) for ref in refs]
            )
            db.execute("INSERT INTO episode_search VALUES (?,?,?)", (eid, title, summary))
            return eid

    def read(self, episode_id: str, *, active_only: bool = True) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if not row:
                raise NotFound("事件不存在")
            if active_only and row["status"] != "active":
                raise SourceInvalid("事件依据已失效，等待重新整理")
            refs = [
                SourceRef(id=r[0], revision=r[1])
                for r in db.execute(
                    "SELECT source_id,source_revision FROM episode_sources WHERE episode_id=?",
                    (episode_id,),
                )
            ]
            if active_only:
                self.history.validate_refs(row["agent_id"], refs, row["scope"])
            return dict(row) | {"sources": [r.model_dump() for r in refs]}

    def find(
        self, agent_id: str, query: str, *, scope: str = "real", limit: int = 20, offset: int = 0
    ) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("无效分页")
        with self.database.transaction() as db:
            params: list = [agent_id, scope]
            sql = (
                "SELECT e.id FROM episodes e WHERE e.agent_id=? AND e.scope=? AND e.status='active'"
            )
            if len(query.strip()) >= 3:
                sql += " AND e.id IN (SELECT episode_id FROM episode_search WHERE episode_search MATCH ?)"
                params.append('"' + query.replace('"', '""') + '"')
            elif query:
                literal = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                sql += " AND (e.title LIKE ? ESCAPE '!' OR e.summary LIKE ? ESCAPE '!')"
                params.extend([f"%{literal}%", f"%{literal}%"])
            sql += " ORDER BY e.created_at DESC,e.id LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [self.read(row[0]) for row in db.execute(sql, params).fetchall()]

    def invalidate(self, ref: SourceRef) -> None:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE episodes SET status='invalidated' WHERE id IN (SELECT episode_id FROM episode_sources WHERE source_id=? AND source_revision=?)",
                (ref.id, ref.revision),
            )
