import hashlib
import json
from collections.abc import Callable

from lingnexus.common.errors import Conflict, NotFound, SourceInvalid
from lingnexus.common.types import SourceInput, SourceKind, SourceRef, new_id, utc_now
from lingnexus.storage.db import Database


class HistoryService:
    def __init__(self, database: Database):
        self.database = database

    def add(
        self, agent_id: str, content: str, *, kind: SourceKind, scope: str = "", request_id: str
    ) -> SourceRef:
        value = SourceInput(content=content, kind=kind, scope=scope, request_id=request_id)
        fingerprint = hashlib.sha256(value.model_dump_json().encode()).hexdigest()
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("个体不存在")
            old = db.execute(
                "SELECT * FROM sources WHERE agent_id=? AND request_id=?", (agent_id, request_id)
            ).fetchone()
            if old:
                if old["request_fingerprint"] != fingerprint:
                    raise Conflict("同一个请求编号不能提交不同内容")
                return SourceRef(id=old["id"], revision=1)
            source_id, now = new_id(), utc_now()
            db.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
                (source_id, agent_id, kind, scope, request_id, fingerprint, 1, now),
            )
            db.execute(
                "INSERT INTO source_revisions VALUES (?,?,?,?)", (source_id, 1, content, now)
            )
            self._enqueue(db, agent_id, SourceRef(id=source_id, revision=1), now)
            return SourceRef(id=source_id, revision=1)

    @staticmethod
    def _enqueue(db, agent_id: str, ref: SourceRef, now: str) -> None:
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (
                new_id(),
                agent_id,
                "consolidate",
                f"source:{ref.id}:{ref.revision}",
                ref.model_dump_json(),
                "queued",
                now,
            ),
        )

    def read(self, ref: SourceRef, *, active_only: bool = True) -> dict:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT s.agent_id,s.kind,s.scope,s.current_revision,r.* FROM sources s "
                "JOIN source_revisions r ON r.source_id=s.id WHERE s.id=? AND r.revision=?",
                (ref.id, ref.revision),
            ).fetchone()
            if row is None:
                raise NotFound("经历版本不存在")
            if active_only and row["current_revision"] != ref.revision:
                raise SourceInvalid("经历已被纠正，请读取当前版本")
            visible = db.execute(
                "SELECT active FROM source_visibility WHERE source_id=? AND source_revision=?",
                (ref.id, ref.revision),
            ).fetchone()
            if active_only and visible is not None and not visible[0]:
                raise SourceInvalid("该内容属于未选中的交流分支")
            return {
                "id": ref.id,
                "revision": ref.revision,
                "agent_id": row["agent_id"],
                "kind": row["kind"],
                "scope": row["scope"],
                "text": row["content"],
                "recorded_at": row["recorded_at"],
            }

    def correct(self, ref: SourceRef, content: str) -> SourceRef:
        if not content.strip():
            raise ValueError("纠正内容不能为空")
        with self.database.transaction() as db:
            original = self.read(ref)
            updated = SourceRef(id=ref.id, revision=ref.revision + 1)
            now = utc_now()
            db.execute(
                "INSERT INTO source_revisions VALUES (?,?,?,?)",
                (ref.id, updated.revision, content, now),
            )
            db.execute(
                "UPDATE sources SET current_revision=? WHERE id=?", (updated.revision, ref.id)
            )
            self._enqueue(db, original["agent_id"], updated, now)
            return updated

    def set_visible(self, ref: SourceRef, visible: bool) -> None:
        with self.database.transaction() as db:
            record = self.read(ref, active_only=False)
            current = db.execute(
                "SELECT current_revision FROM sources WHERE id=?", (ref.id,)
            ).fetchone()[0]
            if visible and current != ref.revision:
                raise SourceInvalid("不能重新启用已被纠正的原始版本")
            old = db.execute(
                "SELECT active FROM source_visibility WHERE source_id=? AND source_revision=?",
                (ref.id, ref.revision),
            ).fetchone()
            was_visible = old is None or bool(old[0])
            if was_visible == visible:
                return
            db.execute(
                "INSERT INTO source_visibility VALUES (?,?,?) ON CONFLICT(source_id,source_revision) DO UPDATE SET active=excluded.active",
                (ref.id, ref.revision, int(visible)),
            )
            if visible:
                db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                    (
                        new_id(),
                        record["agent_id"],
                        "consolidate",
                        "reactivate:" + new_id(),
                        ref.model_dump_json(),
                        "queued",
                        utc_now(),
                    ),
                )

    def validate_refs(
        self, agent_id: str, refs: list[SourceRef], scope: str | None = None
    ) -> list[dict]:
        if not refs:
            raise SourceInvalid("派生资料必须有来源")
        rows = [self.read(ref) for ref in refs]
        if any(row["agent_id"] != agent_id for row in rows):
            raise SourceInvalid("来源属于其他个体")
        return rows

    def count(self, agent_id: str) -> int:
        with self.database.transaction() as db:
            return db.execute(
                "SELECT count(*) FROM sources WHERE agent_id=?", (agent_id,)
            ).fetchone()[0]

    def pending_jobs(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE agent_id=? AND status='queued'", (agent_id,)
            )
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]
