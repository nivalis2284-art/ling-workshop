"""Per-conversation access mode: whether gated calls still wait for the owner.

The owner may hand one conversation a standing answer: with the mode set to
``full``, commands and remote (``mcp__*``) tools stop waiting for a per-call
confirmation *in that conversation only*. The mode changes nothing else — the
sandbox tier, audit, directory grants and browser click approvals all stay
exactly as they are, and every other conversation keeps waiting as before.

The mode lives here; what it *means* is the confirmation hook's business — the
hook reads the mode and releases gated calls with a marker. The chat loop never
reads the mode: the release travels back through the hook result, so the loop
body still holds no policy.
"""

from __future__ import annotations

from lingnexus.common.errors import DomainError, NotFound
from lingnexus.common.types import utc_now

MODES = ("confirm", "full")
DEFAULT_MODE = "confirm"


class ConversationAccess:
    """Reads and writes the access mode of one conversation at a time."""

    def __init__(self, database):
        self.database = database

    def mode(self, conversation_id: str) -> str:
        """The stored mode, or ``confirm`` when nothing was ever stored.

        This is the confirmation hook's read: a conversation that was never
        touched still asks for a per-call decision, exactly as it always did.
        """
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT mode FROM chat_conversation_access WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return row[0] if row else DEFAULT_MODE

    def get(self, conversation_id: str) -> dict:
        """The mode for the owner's UI; unknown conversations are not found."""
        with self.database.transaction() as db:
            if (
                db.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
                is None
            ):
                raise NotFound("交流不存在")
            row = db.execute(
                "SELECT mode FROM chat_conversation_access WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return {
            "conversation_id": conversation_id,
            "mode": row[0] if row else DEFAULT_MODE,
        }

    def set(self, conversation_id: str, mode: str) -> dict:
        """Store the mode idempotently; unknown modes are values, never silent writes."""
        if mode not in MODES:
            raise DomainError("访问模式必须是 confirm 或 full。")
        with self.database.transaction() as db:
            if (
                db.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
                is None
            ):
                raise NotFound("交流不存在")
            db.execute(
                "INSERT INTO chat_conversation_access(conversation_id,mode,updated_at) "
                "VALUES (?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                "mode=excluded.mode, updated_at=excluded.updated_at",
                (conversation_id, mode, utc_now()),
            )
        return {"conversation_id": conversation_id, "mode": mode}
