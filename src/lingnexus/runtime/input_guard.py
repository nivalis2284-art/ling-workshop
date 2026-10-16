"""Admission fence for a frozen chat input; no transcript text leaves this module."""

import hashlib
import json

from lingnexus.common.errors import Conflict, DomainError


def prefix_guards(app, agent_id: str, conversation_id: str, messages: list[dict]) -> dict[str, str]:
    """Hash all user-prefix snapshots in one pass, with the version-1 encoding.

    Completed turns can be replayed on a later head only when their original
    prefix is still on this selected branch with the same authorization.
    """
    browser = getattr(app, "browser", None)
    base = {
        "agent_id": agent_id,
        "conversation_id": conversation_id,
        "grants": app.permissions.current(agent_id),
        "browser": browser.current_origins(agent_id) if browser is not None else None,
    }
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256((encode(base)[:-1] + ',"messages":[').encode("utf-8"))
    result = {}
    for index, message in enumerate(messages):
        node = [message["id"], message.get("parent_id"), message["role"],
                hashlib.sha256(message["text"].encode("utf-8")).hexdigest(),
                message.get("attachment_ids", [])]
        digest.update((("," if index else "") + encode(node)).encode("utf-8"))
        if message["role"] == "user":
            current = digest.copy()
            current.update(b'],"version":1}')
            result[message["id"]] = current.hexdigest()
    return result


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
        return prefix_guards(app, run["agent_id"], conversation["id"], messages)[anchor]


def matches(app, run: dict, expected: str | None) -> bool:
    if not expected:
        # Legacy checkpoints lack enough provenance to prove that an ancestor
        # wasn't deleted; preserve them privately, but require a fresh request.
        return False
    try:
        return capture(app, run) == expected
    except DomainError:
        return False
