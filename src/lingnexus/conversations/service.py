"""Selected conversation paths; original message nodes remain viewable."""

from __future__ import annotations

import json
import re

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied, RevisionConflict
from lingnexus.common.types import SourceRef, new_id, utc_now


class Conversations:
    def __init__(self, database, history):
        self.database, self.history = database, history

    def create(
        self, agent_id: str, title: str, *, scene: str | None = None, background: str = ""
    ) -> dict:
        if not title.strip() or len(background) > 6000 or scene is not None and not scene.strip():
            raise ValueError("交流名称或场景编号无效")
        if scene is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", scene):
            scene = new_id()
        if scene is None and background:
            raise ValueError("背景只用于明确的场景交流")
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("个体不存在")
            cid = new_id()
            db.execute(
                "INSERT INTO conversations(id,agent_id,title,scope,head_id,created_at,background) VALUES (?,?,?,?,?,?,?)",
                (
                    cid,
                    agent_id,
                    title,
                    "scene:" + scene if scene else "real",
                    None,
                    utc_now(),
                    background,
                ),
            )
            return self.read(cid)

    def read(self, conversation_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise NotFound("交流不存在")
            return dict(row)

    def for_chat(self, agent_id: str, conversation_id: str) -> dict:
        value = self.read(conversation_id)
        if value["agent_id"] != agent_id:
            raise PermissionDenied("交流属于其他个体")
        if not re.fullmatch(r"real|scene:[A-Za-z0-9_-]+", value["scope"]):
            raise PermissionDenied("该交流情境不支持聊天")
        return value

    def default_real(self, agent_id: str) -> dict:
        """Bind legacy ID-less clients once, independently of UI selection."""
        key = f"default_conversation:{agent_id}"
        with self.database.transaction() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                value = self.for_chat(agent_id, json.loads(row[0]))
                if value["scope"] != "real":
                    raise PermissionDenied("默认交流必须属于现实情境")
                return value
            value = self.create(agent_id, "日常交流")
            db.execute("INSERT INTO settings VALUES (?,?)", (key, json.dumps(value["id"])))
            return value

    def list(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM conversations WHERE agent_id=? ORDER BY created_at DESC",
                    (agent_id,),
                )
            ]

    def _node(self, conversation_id: str, message_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id=? AND id=?",
                (conversation_id, message_id),
            ).fetchone()
            if row is None:
                raise NotFound("消息不属于该交流")
            ref = SourceRef(id=row["source_id"], revision=row["source_revision"])
            source = self.history.read(ref, active_only=False)
            attachments = [
                value[0]
                for value in db.execute(
                    "SELECT attachment_id FROM conversation_attachments WHERE message_id=? ORDER BY ordinal",
                    (message_id,),
                )
            ]
            return dict(row) | {
                "source": ref,
                "text": source["text"],
                "attachment_ids": attachments,
            }

    def _path(self, conversation_id: str, head: str | None) -> list[dict]:
        nodes, seen = [], set()
        while head:
            if head in seen:
                raise Conflict("消息引用存在循环")
            seen.add(head)
            node = self._node(conversation_id, head)
            nodes.append(node)
            head = node["parent_id"]
        return list(reversed(nodes))

    def messages(self, conversation_id: str, *, head_id: str | None = None) -> list[dict]:
        with self.database.transaction():
            value = self.read(conversation_id)
            return self._path(conversation_id, head_id or value["head_id"])

    def branches(self, conversation_id: str) -> list[dict]:
        """Return branch tips and the selected head, including an interior selection."""
        with self.database.transaction() as db:
            value = self.read(conversation_id)
            rows = db.execute(
                "SELECT m.id FROM conversation_messages m WHERE m.conversation_id=? "
                "AND (m.id=? OR NOT EXISTS (SELECT 1 FROM conversation_messages child "
                "WHERE child.parent_id=m.id)) ORDER BY m.created_at,m.rowid",
                (conversation_id, value["head_id"]),
            ).fetchall()
            return [
                self._node(conversation_id, row["id"]) | {"selected": row["id"] == value["head_id"]}
                for row in rows
            ]

    def _append(
        self,
        conversation_id: str,
        text: str,
        role: str,
        *,
        request_id: str,
        expected_head: str | None = None,
        parent: str | None = None,
        fork: bool = False,
    ) -> dict:
        with self.database.transaction() as db:
            value = self.read(conversation_id)
            old = db.execute(
                "SELECT id FROM conversation_messages WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if old:
                node = self._node(conversation_id, old[0])
                if node["text"] != text or node["role"] != role:
                    raise Conflict("消息请求编号冲突")
                return node
            if expected_head is not None and value["head_id"] != expected_head:
                raise RevisionConflict("交流已经切换到新版本")
            parent_id = parent if fork else value["head_id"]
            if parent_id:
                self._node(conversation_id, parent_id)
            ref = self.history.add(
                value["agent_id"],
                text,
                kind=role,
                scope=value["scope"],
                request_id=f"conversation:{conversation_id}:{request_id}",
            )
            mid = new_id()
            db.execute(
                "INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                (
                    mid,
                    conversation_id,
                    parent_id,
                    ref.id,
                    ref.revision,
                    role,
                    request_id,
                    utc_now(),
                ),
            )
            self.select_head(conversation_id, mid)
            return self._node(conversation_id, mid)

    def append_user(self, conversation_id: str, text: str, *, request_id: str) -> dict:
        return self._append(conversation_id, text, "user", request_id=request_id)

    def append_assistant(
        self, conversation_id: str, text: str, *, request_id: str, expected_head: str
    ) -> dict:
        return self._append(
            conversation_id, text, "assistant", request_id=request_id, expected_head=expected_head
        )

    def edit_user(
        self, conversation_id: str, message_id: str, text: str, *, request_id: str
    ) -> dict:
        with self.database.transaction():
            node = self._node(conversation_id, message_id)
            if node["role"] != "user":
                raise PermissionDenied("不能直接编辑灵的回复")
            return self._append(
                conversation_id,
                text,
                "user",
                request_id=request_id,
                parent=node["parent_id"],
                fork=True,
            )

    def select_head(self, conversation_id: str, message_id: str) -> dict:
        with self.database.transaction() as db:
            value = self.read(conversation_id)
            old = self._path(conversation_id, value["head_id"])
            selected = self._path(conversation_id, message_id)
            selected_ids = {node["id"] for node in selected}
            for node in old:
                if node["id"] not in selected_ids:
                    self.history.set_visible(node["source"], False)
            for node in selected:
                self.history.set_visible(node["source"], True)
            db.execute(
                "UPDATE conversations SET head_id=? WHERE id=?", (message_id, conversation_id)
            )
            return self.read(conversation_id)
