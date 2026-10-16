"""Native model payloads from one selected conversation branch and its attachments.

This module also owns the native shape of a tool exchange, because only the
protocol family knows how a declaration and a result are spelled. The loop
declares tools once, then appends the assistant's tool calls and their results
after every step — in the family's own format, never a normalized one.

The opening carries the caller's environment facts, and every user message
carries its own stored send time: the cache prefix stays stable and only the
tail moves.
"""

import json
import platform
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING

from lingnexus.actions.catalog import registry as tool_registry
from lingnexus.context.builder import BudgetPolicy
from lingnexus.models import APIFamily, ModelProfile, ToolCall

from . import attachments
from .checkpoints import RunCheckpoints
from .input_guard import prefix_guards

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
    # Resolve provenance on the real selected branch, then project *before*
    # native replay, attachments and the ordinary window selection.
    conversation_id = next((m.get("conversation_id") for m in messages if m.get("conversation_id")), None)
    source_guards = None
    event_manifest = []
    if conversation_id:
        source_guards = prefix_guards(app, agent_id, conversation_id, messages)
        from lingnexus.work_events.projection import project_history

        messages, event_manifest = project_history(app, agent_id, conversation_id, messages)
    plans_by_message = {}
    entries = []
    original_turns = {}
    checkpoints = RunCheckpoints(app.database)
    for message in messages:
        if message["role"] == "assistant":
            items = checkpoints.completed_items(
                message["id"], profile.model_dump(mode="json"), source_guards=source_guards
            )
            if items:
                original_turns[message["id"]] = items
        plans = (
            attachments.for_message(app, agent_id, message["id"])
            if message["role"] == "user"
            else []
        )
        plans_by_message[message["id"]] = plans
        stamp = _sent_time(message.get("created_at")) if message["role"] == "user" else ""
        entries.append(
            {
                "id": message["id"],
                "role": message["role"],
                "text": message["text"],
                "stamp": stamp,
                "extra_tokens": sum(plan.estimated_tokens for plan in plans)
                + len(stamp.encode("utf-8"))
                + (len(json.dumps(original_turns[message["id"]], ensure_ascii=False).encode())
                   if message["id"] in original_turns else 0),
            }
        )
    tool_payload = {}
    declare_tools(tool_payload, profile, tool_registry.schema())
    tool_tokens = app.context.counter.count(json.dumps(tool_payload, ensure_ascii=False))
    context = app.context.build(
        agent_id,
        messages=entries,
        budget=BudgetPolicy(context_limit=limit, output_reserve=output, tool_reserve=tool_tokens + 512),
        extra_header=_skill_index(app, profile),
        environment=_environment_block(app, agent_id),
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
        content = attachments.content(app, profile, message["text"], plans)
        stamp = str(message.get("stamp") or "")
        if stamp:
            content = (
                f"{content}\n{stamp}"
                if isinstance(content, str)
                else [*content, attachments.text_block(profile, stamp)]
            )
        if message["id"] in original_turns:
            native.extend(original_turns[message["id"]])
        else:
            native.append({"role": message["role"], "content": content})
    manifest = {
        "agent_id": agent_id,
        "sent_messages": len(selected),
        "truncated_messages": context.manifest["truncated_messages"] + dropped,
        "attachments": attachment_manifest,
        "estimated_input_tokens": context.manifest["estimated_input_tokens"],
        "token_count_exact": context.manifest["token_count_exact"] and not attachment_tokens,
        "strategy_version": 3,
        "work_events": [dict(item, sent=any(m["id"] == item["projection_id"] for m in selected)) for item in event_manifest],
    }
    instructions = context.instructions
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        payload = {"system": instructions, "messages": native, "max_tokens": output}
    elif profile.api_family == APIFamily.OPENAI_RESPONSES:
        payload = {"instructions": instructions, "input": native, "max_output_tokens": output}
        payload["store"] = False
        if profile.capabilities.encrypted_reasoning:
            payload["include"] = ["reasoning.encrypted_content"]
    else:
        payload = {
            "messages": [{"role": "system", "content": instructions}, *native],
            "max_completion_tokens": output,
        }
    return payload, manifest


def _environment_block(app, agent_id: str) -> str:
    """Owner-granted machine facts; a statement of environment, never a policy."""
    grants = app.permissions.current(agent_id)
    writable = [str(path) for path in grants["writable"]]
    readable = [str(path) for path in grants["readable"]]
    lines = [
        "运行环境：",
        f"- 平台：{platform.system() or '未知'}",
        "- 可写目录：" + ("；".join(writable) if writable else "无"),
        "- 可读目录：" + ("；".join(readable) if readable else "无"),
    ]
    if writable:
        from lingnexus.common.workspace_scan import policy

        lines.append("- 工作区验证范围与上限：" + json.dumps(policy(), ensure_ascii=False))
        lines.append("- 命令执行：可用（" + _sandbox_summary(app) + "）")
        lines.append("- 命令工作目录：" + writable[0])
        sandbox = getattr(app, "sandbox", None)
        if sandbox is not None:
            info = sandbox.info()
            limits = info.get("limits", {})
            lines.extend([
                "- 命令额度档位：" + str(info.get("preset", "unknown")),
                "- 执行资源：" + json.dumps(limits, ensure_ascii=False, sort_keys=True),
                "- 命令格式：argv参数列表，不使用shell；按上述平台选择可用程序，不预设Linux命令。",
                "- 已安装依赖：unknown，需要通过已授权的命令检查。",
            ])
    else:
        lines.append("- 命令执行：不可用（没有已授权的可写目录）")
    return "\n".join(lines)


_ENVIRONMENT_COMMANDS = {
    "local": "轻量受限执行：无 shell；有时限、输出与资源上限；不是文件系统或网络隔离",
    "container": "容器隔离：只挂载工作目录、默认无网络",
}


def _sandbox_summary(app) -> str:
    sandbox = getattr(app, "sandbox", None)
    level = sandbox.level() if sandbox is not None else "local"
    return _ENVIRONMENT_COMMANDS.get(level, _ENVIRONMENT_COMMANDS["local"])


def _sent_time(value: str | None) -> str:
    """The stamped send time for one stored user message; "" when unusable.

    Derived from the message's own stored ``created_at``, so the bytes of a
    message never change between turns and a cache prefix only breaks where
    something actually changed. The newest message's stamp is the current time.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return "（发送时间：" + moment.astimezone().strftime("%Y-%m-%d %H:%M") + "）"


def _skill_index(app: "Application", profile: ModelProfile) -> str:
    """The resident skill list (disclosure level 1), or "" when it cannot be used.

    A profile that cannot request tools would only get noise from the list — and
    the index is charged to the request budget — so it is sent only where the
    model can actually act on it. No skills installed means no extra header at
    all, which is what keeps this feature optional.
    """
    if not profile.capabilities.tools:
        return ""
    library = getattr(app, "skillpacks", None)
    return library.prompt_index() if library is not None else ""


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


def append_native_response(payload: dict, family, response: dict) -> None:
    response = deepcopy(response)
    if family == APIFamily.ANTHROPIC_MESSAGES:
        payload["messages"].append({"role": "assistant", "content": response["content"]})
    elif family == APIFamily.OPENAI_RESPONSES:
        payload["input"].extend(response["output"])
    else:
        payload["messages"].append(response["choices"][0]["message"])


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
