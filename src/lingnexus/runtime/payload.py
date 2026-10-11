"""Native model payloads from one selected conversation branch and its attachments.

This module also owns the native shape of a tool exchange, because only the
protocol family knows how a declaration and a result are spelled. The loop
declares tools once, then appends the assistant's tool calls and their results
after every step — in the family's own format, never a normalized one.
"""

from typing import TYPE_CHECKING

from lingnexus.context.builder import BudgetPolicy
from lingnexus.models import APIFamily, ModelProfile, ToolCall

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


def _messages(payload: dict, profile: ModelProfile) -> list:
    """The mutable native turn list for this family (Responses uses ``input``)."""
    return payload["input"] if profile.api_family == APIFamily.OPENAI_RESPONSES else payload["messages"]


def declare_tools(payload: dict, profile: ModelProfile, schema: list[dict]) -> None:
    """Add the tool declarations in this family's native shape, when supported.

    A profile that does not declare tool support never receives declarations —
    the gateway rejects a request that carries tools without that capability.
    """
    if not schema or not profile.capabilities.tools:
        return
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        payload["tools"] = [
            {
                "name": item["name"],
                "description": item["description"],
                "input_schema": item["parameters"],
            }
            for item in schema
        ]
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        payload["tools"] = [
            {
                "type": "function",
                "name": item["name"],
                "description": item["description"],
                "parameters": item["parameters"],
            }
            for item in schema
        ]
    else:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["parameters"],
                },
            }
            for item in schema
        ]


def append_tool_calls(
    payload: dict, profile: ModelProfile, calls: list[ToolCall], text: str = ""
) -> None:
    """Append the assistant's tool-call turn in this family's native shape."""
    items = _messages(payload, profile)
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in calls
        )
        items.append({"role": "assistant", "content": blocks})
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        if text:
            items.append({"role": "assistant", "content": text})
        items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments_json,
            }
            for call in calls
        )
    else:
        items.append(
            {
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments_json},
                    }
                    for call in calls
                ],
            }
        )


def append_tool_results(
    payload: dict, profile: ModelProfile, results: list[tuple[ToolCall, str]]
) -> None:
    """Append one result per call in this family's native shape.

    ``results`` is an ordered list of ``(call, content_text)``; a rejection is
    carried as ordinary content so the model learns why a call did not run.
    """
    items = _messages(payload, profile)
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        items.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": text}
                    for call, text in results
                ],
            }
        )
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        items.extend(
            {"type": "function_call_output", "call_id": call.id, "output": text}
            for call, text in results
        )
    else:
        items.extend(
            {"role": "tool", "tool_call_id": call.id, "content": text}
            for call, text in results
        )


def append_user_message(payload: dict, profile: ModelProfile, text: str) -> None:
    """Inject a mid-run guidance message as an ordinary user turn."""
    _messages(payload, profile).append({"role": "user", "content": text})
