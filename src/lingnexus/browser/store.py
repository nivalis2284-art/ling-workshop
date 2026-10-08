"""Committed browser requests and immutable, one-use owner approvals."""

import hashlib
import json

from lingnexus.browser.policy import ActionInput, OriginGrant
from lingnexus.common.errors import Conflict, NotFound, RevisionConflict
from lingnexus.common.types import new_id, utc_now


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


class BrowserStore:
    def __init__(self, database):
        self.database = database

    def origins(self, agent_id: str) -> dict:
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("Agent does not exist")
            row = db.execute(
                "SELECT * FROM browser_origin_revisions WHERE agent_id=? ORDER BY revision DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            return {
                "agent_id": agent_id,
                "revision": row["revision"] if row else 0,
                "grants": json.loads(row["grants"]) if row else [],
            }

    def configure(self, agent_id: str, grants: list[OriginGrant], expected_revision: int) -> dict:
        with self.database.transaction() as db:
            if self.origins(agent_id)["revision"] != expected_revision:
                raise RevisionConflict("Browser grants changed; inspect current state")
            db.execute(
                "INSERT INTO browser_origin_revisions VALUES (?,?,?,?)",
                (
                    agent_id,
                    expected_revision + 1,
                    encode([g.model_dump() for g in grants]),
                    utc_now(),
                ),
            )
            pending = db.execute(
                "SELECT action_id FROM browser_approvals WHERE agent_id=? AND status='pending'",
                (agent_id,),
            ).fetchall()
            for row in pending:
                self.invalidate(row["action_id"], "origin_changed")
            return self.origins(agent_id)

    def find(self, agent_id: str, request: ActionInput) -> dict | None:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT id,fingerprint FROM browser_actions WHERE agent_id=? AND request_id=?",
                (agent_id, request.request_id),
            ).fetchone()
            if row and row["fingerprint"] != digest(request.model_dump()):
                raise Conflict("Browser request_id is already bound to another intent")
            return self.get(row["id"]) if row else None

    def prepare(self, agent_id: str, request: ActionInput) -> dict:
        with self.database.transaction() as db:
            self.origins(agent_id)
            old = self.find(agent_id, request)
            if old:
                return old
            action_id, now = new_id(), utc_now()
            db.execute(
                "INSERT INTO browser_actions VALUES (?,?,?,?,?,?,'prepared',NULL,NULL,?,?)",
                (
                    action_id,
                    agent_id,
                    request.request_id,
                    request.action,
                    encode(request.model_dump()),
                    digest(request.model_dump()),
                    now,
                    now,
                ),
            )
            return self.get(action_id)

    def get(self, action_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM browser_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise NotFound("Browser action does not exist")
            item = dict(row)
            item["arguments"] = json.loads(item["arguments"])
            item["result"] = json.loads(item["result"]) if item["result"] else None
            approval = db.execute(
                "SELECT id FROM browser_approvals WHERE action_id=?", (action_id,)
            ).fetchone()
            item["approval_id"] = approval["id"] if approval else None
            return item

    def request_approval(self, action_id: str, intent: dict) -> dict:
        with self.database.transaction() as db:
            action = self.get(action_id)
            db.execute(
                "INSERT INTO browser_approvals VALUES (?,?,?,?,?,'pending',?,NULL)",
                (
                    new_id(),
                    action_id,
                    action["agent_id"],
                    encode(intent),
                    digest(intent),
                    utc_now(),
                ),
            )
            return self.get(action_id)

    def approval(self, approval_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM browser_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Browser approval does not exist")
            item = dict(row)
            item["intent"] = json.loads(item["intent"])
            return item

    def pending(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            self.origins(agent_id)
            rows = db.execute(
                "SELECT id FROM browser_approvals WHERE agent_id=? AND status='pending' ORDER BY created_at",
                (agent_id,),
            ).fetchall()
            return [self.approval(row["id"]) for row in rows]

    def mark_executing(self, action_id: str) -> bool:
        with self.database.transaction() as db:
            return (
                db.execute(
                    "UPDATE browser_actions SET status='executing',updated_at=? WHERE id=? AND status='prepared'",
                    (utc_now(), action_id),
                ).rowcount
                == 1
            )

    def approve(self, approval_id: str) -> bool:
        with self.database.transaction() as db:
            approval = self.approval(approval_id)
            if approval["status"] != "pending":
                return False
            if (
                self.origins(approval["agent_id"])["revision"]
                != approval["intent"]["origin_revision"]
            ):
                self.invalidate(approval["action_id"], "origin_changed")
                return False
            if not self.mark_executing(approval["action_id"]):
                return False
            db.execute(
                "UPDATE browser_approvals SET status='approved',decided_at=? WHERE id=?",
                (utc_now(), approval_id),
            )
            return True

    def finish(
        self, action_id: str, status: str, result: dict | None = None, error_code: str | None = None
    ) -> dict:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE browser_actions SET status=?,result=?,error_code=?,updated_at=? "
                "WHERE id=? AND status='executing'",
                (
                    status,
                    encode(result) if result is not None else None,
                    error_code,
                    utc_now(),
                    action_id,
                ),
            )
            return self.get(action_id)

    def invalidate(self, action_id: str, reason: str, *, rejected: bool = False) -> dict:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE browser_approvals SET status=?,decided_at=? WHERE action_id=? AND status='pending'",
                ("rejected" if rejected else "invalidated", utc_now(), action_id),
            )
            db.execute(
                "UPDATE browser_actions SET status='failed',error_code=?,updated_at=? "
                "WHERE id=? AND status='prepared'",
                (reason, utc_now(), action_id),
            )
            return self.get(action_id)

    def recover(self) -> None:
        """Called once by the single parent lifespan, before accepting actions."""
        with self.database.transaction() as db:
            db.execute(
                "UPDATE browser_actions SET status='unknown',error_code='interrupted_execution',"
                "updated_at=? WHERE status='executing'",
                (utc_now(),),
            )
            rows = db.execute("SELECT id FROM browser_actions WHERE status='prepared'").fetchall()
            for row in rows:
                self.invalidate(row["id"], "browser_session_ended")
