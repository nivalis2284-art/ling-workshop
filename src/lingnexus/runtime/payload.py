"""Native model payloads from one selected conversation branch and its attachments."""

from typing import TYPE_CHECKING

from lingnexus.context.builder import BudgetPolicy
from lingnexus.models import APIFamily, ModelProfile

from . import attachments

if TYPE_CHECKING:
    from lingnexus.app import Application


def build_payload(
    app: "Application",
    agent_id: str,
    profile: ModelProfile,
    messages: list[dict],
) -> tuple[dict, dict]:
    """Return the native request plus a manifest of what was sent and truncated.

    `messages` is the selected branch in order, each `{id, role, text}`. Attachments
    ride on user messages. The newest contiguous tail that fits the window is sent;
    older messages are dropped and reported instead of failing the turn.
    """
    output = profile.max_output_tokens or 4096
    limit = profile.context_window_tokens or 32768
    plans_by_message = {}
    entries = []
    for message in messages:
        plans = (
            attachments.for_message(app, agent_id, message["id"])
            if message["role"] == "user"
            else []
        )
        plans_by_message[message["id"]] = plans
        entries.append(
            {
                "id": message["id"],
                "role": message["role"],
                "text": message["text"],
                "extra_tokens": sum(plan.estimated_tokens for plan in plans),
            }
        )
    context = app.context.build(
        agent_id,
        messages=entries,
        budget=BudgetPolicy(context_limit=limit, output_reserve=output, tool_reserve=256),
    )
    # Native attachments are bytes-bounded independently of the token estimate.
    selected = list(context.evidence)
    native_bytes = sum(
        plan.native_bytes for message in selected for plan in plans_by_message[message["id"]]
    )
    dropped = 0
    while selected and native_bytes > attachments.MAX_NATIVE_BYTES:
        removed = selected.pop(0)
        native_bytes -= sum(
            plan.native_bytes for plan in plans_by_message[removed["id"]]
        )
        dropped += 1
    if not selected and entries:
        selected = [entries[-1]]
    native, attachment_manifest, attachment_tokens = [], [], 0
    for message in selected:
        plans = plans_by_message[message["id"]]
        attachment_tokens += sum(plan.estimated_tokens for plan in plans)
        attachment_manifest.extend(plan.manifest(message["id"]) for plan in plans)
        native.append(
            {
                "role": message["role"],
                "content": attachments.content(app, profile, message["text"], plans),
            }
        )
    manifest = {
        "agent_id": agent_id,
        "sent_messages": len(selected),
        "truncated_messages": context.manifest["truncated_messages"] + dropped,
        "attachments": attachment_manifest,
        "estimated_input_tokens": (
            context.manifest["estimated_input_tokens"] + attachment_tokens
        ),
        "token_count_exact": context.manifest["token_count_exact"] and not attachment_tokens,
        "strategy_version": 2,
    }
    instructions = context.instructions
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        payload = {"system": instructions, "messages": native, "max_tokens": output}
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        payload = {"instructions": instructions, "input": native, "max_output_tokens": output}
    else:
        payload = {
            "messages": [{"role": "system", "content": instructions}, *native],
            "max_completion_tokens": output,
        }
    return payload, manifest
