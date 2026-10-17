"""Server-owned main conversation binding with explicit legacy selection."""

from __future__ import annotations

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict
from lingnexus.common.types import utc_now


class MainConversation:
    def __init__(self, app):
        self.app = app
        self.database = app.database

    def __call__(self, agent_id):
        return self.get(agent_id)

    def _row(self, agent_id: str):
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("个体不存在。")
            return db.execute(
                "SELECT * FROM continuity_main_conversations WHERE agent_id=?", (agent_id,)
            ).fetchone()

    def get(self, agent_id: str) -> dict:
        row = self._row(agent_id)
        choices = [c for c in self.app.conversations.list(agent_id)
                   if not self.app.continuity.is_work_conversation(c['id'])]
        selected = self.app.conversations.read(row["conversation_id"]) if row else None
        return {
            "conversation": selected,
            "revision": row["revision"] if row else 0,
            "choices": choices,
        }

    def bind(self, agent_id: str, conversation_id: str | None, expected_revision: int) -> dict:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("主干绑定版本无效。")
        with self.database.transaction():
            row = self._row(agent_id)
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise RevisionConflict("主干绑定已变化，请刷新后重试。")
            chosen = None
            if conversation_id is None:
                chosen = self.app.conversations.create(agent_id, "日常交流")
            else:
                chosen = self.app.conversations.for_chat(agent_id, conversation_id)
                if self.app.continuity.is_work_conversation(conversation_id):
                    raise Conflict("工作交流不能绑定为产品主干。")
            if row is None:
                with self.database.transaction() as db:
                    now = utc_now()
                    db.execute(
                        "INSERT INTO continuity_main_conversations(agent_id,conversation_id,revision,created_at,updated_at) "
                        "VALUES(?,?,1,?,?)",
                        (agent_id, chosen["id"], now, now),
                    )
            else:
                with self.database.transaction() as db:
                    db.execute(
                        "UPDATE continuity_main_conversations SET conversation_id=?,revision=revision+1,updated_at=? "
                        "WHERE agent_id=? AND revision=?",
                        (chosen["id"], utc_now(), agent_id, expected_revision),
                    )
        return self.get(agent_id)

    def conversation(self, agent_id: str) -> dict:
        row = self._row(agent_id)
        if row is None:
            raise Conflict("这个灵尚未选择主干交流。")
        return self.app.conversations.for_chat(agent_id, row["conversation_id"])

    def binding(self, agent_id: str) -> dict | None:
        row = self._row(agent_id)
        return dict(row) if row else None
