import hashlib
import json
from typing import Literal

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict, SourceInvalid
from lingnexus.common.types import SourceRef, new_id, utc_now
from lingnexus.identity.policy import ALL_KINDS, may_apply_automatically, validate_experience
from lingnexus.identity.revisions import capture_origins


class Changes:
    """Operations on proposals, injected with the public history and state interfaces."""

    def __init__(self, database, history, current):
        self.database, self.history, self.current = database, history, current

    def get(self, proposal_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise NotFound("候选不存在")
            refs = db.execute(
                "SELECT source_id,source_revision FROM proposal_sources WHERE proposal_id=?",
                (proposal_id,),
            )
            return dict(row) | {
                "text": row["content"],
                "key": row["entry_key"],
                "sources": [{"id": r[0], "revision": r[1]} for r in refs],
            }

    def propose(
        self,
        agent_id: str,
        *,
        kind: str,
        key: str,
        text: str,
        sources: list[SourceRef],
        base_revision: int,
        request_id: str,
    ) -> dict:
        if kind not in ALL_KINDS or not key.strip() or not text.strip() or not request_id:
            raise ValueError("候选类型、内容和请求编号必须有效")
        if key.startswith(("owner.", "permissions.", "system.")):
            raise ValueError("自我变化不能修改系统或所有者权限")
        refs = sorted({(r.id, r.revision) for r in sources})
        fingerprint = hashlib.sha256(
            json.dumps([kind, key, text, refs, base_revision], ensure_ascii=False).encode()
        ).hexdigest()
        with self.database.transaction() as db:
            existing = db.execute(
                "SELECT id,fingerprint FROM proposals WHERE agent_id=? AND request_id=?",
                (agent_id, request_id),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise Conflict("候选请求编号已用于不同内容")
                return self.get(existing["id"])
            records = validate_experience(self.history, agent_id, sources)
            state = self.current(agent_id)
            if state.revision != base_revision:
                raise RevisionConflict("自我状态已经变化，请重新评估候选")
            pid = new_id()
            db.execute(
                "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    agent_id,
                    request_id,
                    fingerprint,
                    base_revision,
                    kind,
                    key,
                    text,
                    "awaiting_owner",
                    utc_now(),
                ),
            )
            db.executemany(
                "INSERT INTO proposal_sources VALUES (?,?,?)", [(pid, *ref) for ref in refs]
            )
            if may_apply_automatically(kind, key, text) and all(
                r["scope"] == "real" for r in records
            ):
                self._apply(self.get(pid), "applied_auto", "system")
            return self.get(pid)

    def decide(
        self, proposal_id: str, decision: Literal["approve", "reject"], *, expected_revision: int
    ) -> dict:
        # Only the owner-facing application/API exposes this operation, never the model toolset.
        if decision not in {"approve", "reject"}:
            raise ValueError("未知审阅操作")
        with self.database.transaction() as db:
            item = self.get(proposal_id)
            if item["status"] == "stale":
                raise SourceInvalid("候选依据已失效")
            if item["status"] in {"applied_owner", "rejected"}:
                if (item["status"] == "applied_owner") == (decision == "approve"):
                    return item
                raise Conflict("该候选已经处理")
            if item["status"] != "awaiting_owner":
                raise Conflict("候选不处于等待审阅状态")
            validate_experience(
                self.history, item["agent_id"], [SourceRef(**r) for r in item["sources"]]
            )
            state = self.current(item["agent_id"])
            if expected_revision != state.revision or (
                decision == "approve" and item["base_revision"] != state.revision
            ):
                raise RevisionConflict("当前版本与候选不一致，需要重新评估")
            if decision == "approve":
                self._apply(item, "applied_owner", "owner")
            else:
                db.execute("UPDATE proposals SET status='rejected' WHERE id=?", (proposal_id,))
                db.execute(
                    "INSERT INTO decisions VALUES (?,?,?,?,?)",
                    (new_id(), proposal_id, "reject", "owner", utc_now()),
                )
            return self.get(proposal_id)

    def _apply(self, item: dict, status: str, actor: str) -> None:
        with self.database.transaction() as db:
            state = self.current(item["agent_id"])
            if state.revision != item["base_revision"]:
                raise RevisionConflict("状态版本已变化")
            entries = state.entries | {item["key"]: item["text"]}
            db.execute(
                "INSERT INTO self_revisions VALUES (?,?,?,?,?)",
                (
                    state.agent_id,
                    state.revision + 1,
                    json.dumps(entries, ensure_ascii=False),
                    item["id"],
                    utc_now(),
                ),
            )
            db.execute(
                "INSERT INTO self_entry_origins VALUES (?,?,?) ON CONFLICT(agent_id,entry_key) DO UPDATE SET proposal_id=excluded.proposal_id",
                (state.agent_id, item["key"], item["id"]),
            )
            db.execute("UPDATE proposals SET status=? WHERE id=?", (status, item["id"]))
            capture_origins(db, state.agent_id, state.revision + 1)
            db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?)",
                (new_id(), item["id"], "apply", actor, utc_now()),
            )

    def remove(self, agent_id: str, *, key: str, expected_revision: int) -> dict:
        """Owner deletion: a new revision omits the entry, every older one stays intact."""
        with self.database.transaction() as db:
            state = self.current(agent_id)
            if state.revision != expected_revision:
                raise RevisionConflict("当前版本已变化")
            if key not in state.entries:
                raise NotFound("这项认识不存在")
            entries = {k: v for k, v in state.entries.items() if k != key}
            db.execute(
                "INSERT INTO self_revisions VALUES (?,?,?,?,?)",
                (
                    agent_id,
                    state.revision + 1,
                    json.dumps(entries, ensure_ascii=False),
                    "owner_remove:" + key,
                    utc_now(),
                ),
            )
            db.execute(
                "DELETE FROM self_entry_origins WHERE agent_id=? AND entry_key=?", (agent_id, key)
            )
            capture_origins(db, agent_id, state.revision + 1)
            return self.current(agent_id)

    def invalidate(self, ref: SourceRef) -> None:
        with self.database.transaction() as db:
            affected = db.execute(
                "SELECT p.id,p.agent_id FROM proposals p JOIN proposal_sources s ON s.proposal_id=p.id WHERE s.source_id=? AND s.source_revision=?",
                (ref.id, ref.revision),
            ).fetchall()
            changed: dict[str, dict[str, str]] = {}
            for row in affected:
                origins = db.execute(
                    "SELECT entry_key FROM self_entry_origins WHERE proposal_id=?", (row["id"],)
                ).fetchall()
                if origins:
                    entries = changed.setdefault(
                        row["agent_id"], dict(self.current(row["agent_id"]).entries)
                    )
                    for origin in origins:
                        entries.pop(origin["entry_key"], None)
                    db.execute("DELETE FROM self_entry_origins WHERE proposal_id=?", (row["id"],))
                db.execute(
                    "UPDATE proposals SET status='stale' WHERE id=? AND status!='rejected'",
                    (row["id"],),
                )
            for agent_id, entries in changed.items():
                state = self.current(agent_id)
                db.execute(
                    "INSERT INTO self_revisions VALUES (?,?,?,?,?)",
                    (
                        agent_id,
                        state.revision + 1,
                        json.dumps(entries, ensure_ascii=False),
                        "source_corrected:" + ref.id,
                        utc_now(),
                    ),
                )
                capture_origins(db, agent_id, state.revision + 1)

    def list(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            return [
                self.get(row[0])
                for row in db.execute(
                    "SELECT id FROM proposals WHERE agent_id=? ORDER BY created_at", (agent_id,)
                ).fetchall()
            ]
