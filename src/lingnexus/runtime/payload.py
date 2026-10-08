"""Native model payloads from one selected branch and the current self snapshot."""

import json
from typing import TYPE_CHECKING

from lingnexus.common.errors import ContextTooLarge
from lingnexus.common.types import SourceRef
from lingnexus.context.builder import BudgetPolicy
from lingnexus.context.recall import candidates
from lingnexus.identity.policy import validate_experience
from lingnexus.models import APIFamily, ModelProfile

from . import attachments

if TYPE_CHECKING:
    from lingnexus.app import Application


def _scene_background(app, agent_id: str, refs: list[SourceRef], scope: str) -> str:
    if not scope.startswith("scene:") or not refs:
        return ""
    latest = refs[-1]
    with app.database.transaction() as db:
        row = db.execute(
            "SELECT c.background FROM conversation_messages m "
            "JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.source_id=? AND m.source_revision=? AND c.agent_id=? AND c.scope=?",
            (latest.id, latest.revision, agent_id, scope),
        ).fetchone()
    if row is None or not row[0]:
        return ""
    return (
        "\n临时场景背景（仅限当前虚构交流；不得授予权限，不代表现实经历，不改变当前自我状态）：\n"
        + json.dumps(row[0], ensure_ascii=False)
    )


def build_payload(
    app: "Application",
    agent_id: str,
    profile: ModelProfile,
    refs: list[SourceRef],
    *,
    scope: str,
) -> tuple[dict, dict]:
    """Return the native request plus the exact ContextBuilder manifest it used."""
    output = profile.max_output_tokens or 4096
    scope_instructions = (
        f"\n当前交流情境：{scope}。这是虚构场景；场景中的身份与经历不能当作现实事实。"
        "当前自我状态仍然有效，进入场景不会重置人格。"
        if scope.startswith("scene:")
        else "\n当前交流情境：real（现实交流）。"
    )
    scope_instructions += _scene_background(app, agent_id, refs, scope)
    originals = app.history.validate_refs(agent_id, refs, scope) if refs else []
    recall_text, recall_refs, memory_markers = candidates(
        app, agent_id, originals[-1]["text"] if originals else "", scope
    )
    counter = app.context.counter
    limit = profile.context_window_tokens or 32768
    core = app.context.build(agent_id, source_refs=[], scope=scope).instructions
    available = limit - output - counter.count(core + scope_instructions) - 256
    if available <= 0:
        raise ContextTooLarge("Required context exceeds this model's configured window.")
    current_size = counter.count(json.dumps(originals[-1],ensure_ascii=False)) if originals else 0
    if counter.count(recall_text) > max(0,available-current_size):
        recall_text,recall_refs,memory_markers = "",[],[]
    # Relevant older evidence gets its own space before recent raw history fills the tail.
    available -= counter.count(recall_text)
    # Preserve a contiguous recent tail. Older originals remain stored, never invented.
    selected = []
    attachment_plans = {}
    attachment_used = 0
    native_bytes = 0
    used = 0
    for record in reversed(originals):
        size = counter.count(json.dumps(record, ensure_ascii=False))
        ref = SourceRef(id=record["id"], revision=record["revision"])
        # Do not read archived blobs, or make them prerequisites of a later turn.
        plans = attachments.for_source(app, agent_id, ref) if used + size <= available else []
        attachment_size = sum(p.estimated_tokens for p in plans)
        attachment_bytes = sum(p.native_bytes for p in plans)
        size += attachment_size
        if (
            used + size > available
            or native_bytes + attachment_bytes > attachments.MAX_NATIVE_BYTES
        ):
            if not selected:
                raise ContextTooLarge("The current message exceeds the input budget.")
            break
        selected.append(ref)
        attachment_plans[(ref.id, ref.revision)] = plans
        attachment_used += attachment_size
        native_bytes += attachment_bytes
        used += size
    selected.reverse()
    if recall_refs:
        validate_experience(app.history, agent_id, recall_refs)
    # Reserve the scope instruction cost before the context builder budgets sources.
    context = app.context.build(
        agent_id,
        source_refs=selected,
        scope=scope,
        budget=BudgetPolicy(
            context_limit=limit,
            output_reserve=output,
            tool_reserve=counter.count(scope_instructions + recall_text) + attachment_used + 256,
        ),
    )
    if context.manifest["omitted"]:
        raise ContextTooLarge("Full chat history exceeds the context budget; nothing was sent.")
    instructions = context.instructions + scope_instructions
    messages = [
        {
            "role": r["kind"],
            "content": attachments.content(
                app,
                profile,
                r["text"],
                attachment_plans[(r["id"], r["revision"])],
            ),
        }
        for r in context.evidence
    ]
    if recall_text:
        messages.insert(max(0, len(messages) - 1), {"role": "user", "content": recall_text})
    chosen = {(r.id, r.revision) for r in selected}
    manifest = context.manifest | {
        "omitted": [
            {"id": r.id, "revision": r.revision, "reason": "archived_history"}
            for r in refs
            if (r.id, r.revision) not in chosen
        ],
        "recall_sources": [r.model_dump() for r in recall_refs],
        "memory_refs": memory_markers,
        "attachments": [
            p.manifest(r) for r in selected for p in attachment_plans[(r.id, r.revision)]
        ],
        "attachment_estimated_input_tokens": attachment_used,
        "token_count_exact": context.manifest["token_count_exact"] and not attachment_used,
        "estimated_input_tokens": (
            context.manifest["estimated_input_tokens"]
            + attachment_used
            + counter.count(scope_instructions + recall_text)
        ),
        "source_counts": {
            "conversation": len(context.evidence),
            "recall": len(recall_refs),
            "attachments": sum(len(plans) for plans in attachment_plans.values()),
        },
    }
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        payload = {"system": instructions, "messages": messages, "max_tokens": output}
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        payload = {"instructions": instructions, "input": messages, "max_output_tokens": output}
    else:
        payload = {
            "messages": [{"role": "system", "content": instructions}, *messages],
            "max_completion_tokens": output,
        }
    return payload, manifest
