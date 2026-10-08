"""Revisable presentation/topic boundaries; never a reset of identity or authority."""

import re
from datetime import UTC, datetime

from lingnexus.common.errors import NotFound, RevisionConflict
from lingnexus.common.types import new_id


def _terms(text):
    text = re.sub(r"\W+", "", text.casefold())
    return {text[i : i + 2] for i in range(max(1, len(text) - 1))}


class Segments:
    def __init__(self, app):
        self.app = app

    def list(self, conversation_id):
        with self.app.database.transaction() as db:
            path = {
                m["id"]: i for i, m in enumerate(self.app.conversations.messages(conversation_id))
            }
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM conversation_segments WHERE conversation_id=?",
                    (conversation_id,),
                )
            ]
            return sorted(
                [r for r in rows if r["start_message_id"] in path],
                key=lambda r: path[r["start_message_id"]],
            )

    def observe(self, conversation_id, message_id, title, *, now=None, force=False):
        if not title.strip() or len(title) > 200:
            raise ValueError("主题标题无效")
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("时间需要时区")
        with self.app.database.transaction() as db:
            path = self.app.conversations.messages(conversation_id)
            indexes = {m["id"]: i for i, m in enumerate(path)}
            if message_id not in indexes:
                raise NotFound("消息不在选定分支")
            segments = self.list(conversation_id)
            containing = [
                r for r in segments if indexes[r["start_message_id"]] <= indexes[message_id]
            ]
            current = containing[-1] if containing else None
            topic_changed = False
            if current:
                a, b = _terms(current["title"]), _terms(title)
                topic_changed = (
                    len(a & b) / max(1, len(a | b)) < 0.15
                    and indexes[message_id] - indexes[current["start_message_id"]] >= 4
                )
                gap = (
                    now - datetime.fromisoformat(current["updated_at"])
                ).total_seconds() > 6 * 3600
                if current["origin"] == "owner" and not force:
                    topic_changed = False
                if not force and not gap and not topic_changed:
                    db.execute(
                        "UPDATE conversation_segments SET last_message_id=?,updated_at=? WHERE id=?",
                        (message_id, now.isoformat(), current["id"]),
                    )
                    return dict(
                        db.execute(
                            "SELECT * FROM conversation_segments WHERE id=?", (current["id"],)
                        ).fetchone()
                    )
            sid = new_id()
            db.execute(
                "INSERT INTO conversation_segments VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    conversation_id,
                    title,
                    message_id,
                    message_id,
                    1,
                    "owner" if force else "automatic",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return dict(
                db.execute("SELECT * FROM conversation_segments WHERE id=?", (sid,)).fetchone()
            )

    def split(self, conversation_id, message_id, title):
        return self.observe(conversation_id, message_id, title, force=True)

    def rename(self, segment_id, title, *, expected_revision):
        if not title.strip() or len(title) > 200:
            raise ValueError("主题标题无效")
        with self.app.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM conversation_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if not row:
                raise NotFound("交流段不存在")
            if row["revision"] != expected_revision:
                raise RevisionConflict("交流段已修改")
            db.execute(
                "UPDATE conversation_segments SET title=?,origin='owner',revision=revision+1 WHERE id=?",
                (title, segment_id),
            )
            return dict(
                db.execute(
                    "SELECT * FROM conversation_segments WHERE id=?", (segment_id,)
                ).fetchone()
            )

    def source_event(self, source_ref, title):
        with self.app.database.transaction() as db:
            row = db.execute(
                "SELECT conversation_id,id,created_at FROM conversation_messages WHERE source_id=? AND source_revision=?",
                (source_ref.id, source_ref.revision),
            ).fetchone()
            if row:
                return self.observe(
                    row["conversation_id"],
                    row["id"],
                    title,
                    now=datetime.fromisoformat(row["created_at"]),
                )
        return None
