"""Admission fence for a frozen chat input; no transcript text leaves this module."""

import hashlib
import json

from lingnexus.common.errors import Conflict, DomainError


def capture(app, run: dict) -> str:
    """Bind a request to its live user anchor, ancestry and authorization revision.

    A deleted anchor is never equivalent to an empty conversation head. Keeping
    a private checkpoint for audit does not grant permission to replay its input.
    """
    with app.database.transaction():
        conversation = app.conversations.for_chat(run["agent_id"], run["conversation_id"])
        anchor = run.get("user_message_id")
        if not anchor or conversation["head_id"] != anchor:
            raise Conflict("原对话已被删除或切换，不能续跑旧输入。")
        messages = app.conversations.messages(conversation["id"])
        if not messages or messages[-1]["id"] != anchor or messages[-1]["role"] != "user":
            raise Conflict("原用户消息已失效，不能续跑旧输入。")
        browser = getattr(app, "browser", None)
        value = {
            "version": 1,
            "agent_id": run["agent_id"],
            "conversation_id": conversation["id"],
            "messages": [
                [m["id"], m["parent_id"], m["role"],
                 hashlib.sha256(m["text"].encode("utf-8")).hexdigest(),
                 m.get("attachment_ids", [])]
                for m in messages
            ],
            "grants": app.permissions.current(run["agent_id"]),
            "browser": browser.current_origins(run["agent_id"]) if browser is not None else None,
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def matches(app, run: dict, expected: str | None) -> bool:
    if not expected:
        # Legacy checkpoints lack enough provenance to prove that an ancestor
        # wasn't deleted; preserve them privately, but require a fresh request.
        return False
    try:
        return capture(app, run) == expected
    except DomainError:
        return False
