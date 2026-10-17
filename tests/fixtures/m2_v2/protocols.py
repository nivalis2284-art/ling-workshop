"""Small native protocol responses for the three supported model transports.

The builders return stream text, not parsed model events.  This is important:
the acceptance tests feed these bytes through ``httpx.MockTransport`` and the
real ``ModelGateway`` parser, so a malformed fixture cannot be hidden by a
second, test-only decoder.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from lingnexus.models import APIFamily, ModelProfile


def _sse(*events: dict[str, Any], done: bool = False) -> str:
    body = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
    return body + ("data: [DONE]\n\n" if done else "")


def chat_text(text: str) -> str:
    """One OpenAI Chat Completions text turn, including a usage-only tail."""
    return _sse(
        {
            "id": "chat-fixture",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chat-fixture",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chat-fixture",
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": {"prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20},
        },
        done=True,
    )


def chat_tools(calls: Iterable[tuple[str, str, dict[str, Any]]], text: str = "") -> str:
    """One Chat Completions turn requesting function calls.

    ``calls`` items are ``(call_id, name, arguments)``.  The arguments remain a
    JSON object encoded as a string, matching the native wire format.
    """
    values = [
        {
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }
        for index, (call_id, name, arguments) in enumerate(calls)
    ]
    delta: dict[str, Any] = {"role": "assistant", "content": text or None}
    if values:
        delta["tool_calls"] = values
    return _sse(
        {
            "id": "chat-fixture-tools",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "tool_calls" if values else "stop",
                }
            ],
        },
        {
            "id": "chat-fixture-tools",
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": {"prompt_tokens": 21, "completion_tokens": 4, "total_tokens": 25},
        },
        done=True,
    )


def anthropic_text(text: str, *, thinking: str | None = None) -> str:
    """One Anthropic Messages turn with a complete content-block sequence."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-fixture",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "fixture-anthropic",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 17, "output_tokens": 0},
            },
        }
    ]
    index = 0
    if thinking is not None:
        events.extend(
            [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": "sig-fixture"},
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": thinking},
                },
                {"type": "content_block_stop", "index": index},
            ]
        )
        index += 1
    events.extend(
        [
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
            {"type": "content_block_stop", "index": index},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"input_tokens": 17, "output_tokens": 3},
            },
            {"type": "message_stop"},
        ]
    )
    return _sse(*events)


def anthropic_tools(
    calls: Iterable[tuple[str, str, dict[str, Any]]], text: str = ""
) -> str:
    """Anthropic tool_use blocks, followed by a complete message stop."""
    content = [
        {
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": arguments,
        }
        for call_id, name, arguments in calls
    ]
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-fixture-tools",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "fixture-anthropic",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 19, "output_tokens": 0},
            },
        }
    ]
    index = 0
    if text:
        events.extend(
            [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": text},
                },
                {"type": "content_block_stop", "index": index},
            ]
        )
        index += 1
    for call in content:
        events.extend(
            [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "tool_use", **call},
                },
                {"type": "content_block_stop", "index": index},
            ]
        )
        index += 1
    events.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"input_tokens": 19, "output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
    )
    return _sse(*events)


def responses_text(text: str) -> str:
    """One Responses API text turn with a final completed response."""
    response = {
        "id": "resp-fixture",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg-fixture",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 17, "output_tokens": 3, "total_tokens": 20},
    }
    return _sse(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "msg-fixture",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg-fixture",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {"type": "response.completed", "response": response},
    )


def responses_tools(
    calls: Iterable[tuple[str, str, dict[str, Any]]], text: str = ""
) -> str:
    """Responses function_call output items and their final completed shape."""
    output: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if text:
        item = {
            "type": "message",
            "id": "msg-fixture-tools",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": item | {"status": "in_progress", "content": []},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg-fixture-tools",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            ]
        )
        output.append(item)
    for call_id, name, arguments in calls:
        item = {
            "type": "function_call",
            "id": f"item-{call_id}",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
            "status": "completed",
        }
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": len(output),
                    "item": item | {"arguments": "", "status": "in_progress"},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": len(output),
                    "delta": item["arguments"],
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": len(output),
                    "arguments": item["arguments"],
                },
                {
                    "type": "response.output_item.done",
                    "output_index": len(output),
                    "item": item,
                },
            ]
        )
        output.append(item)
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": "resp-fixture-tools",
                "object": "response",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 19, "output_tokens": 4, "total_tokens": 23},
            },
        }
    )
    return _sse(*events)


def response_for(family: APIFamily, text: str) -> str:
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        return chat_text(text)
    if family == APIFamily.ANTHROPIC_MESSAGES:
        return anthropic_text(text)
    return responses_text(text)


def tool_response_for(
    family: APIFamily, calls: Iterable[tuple[str, str, dict[str, Any]]], text: str = ""
) -> str:
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        return chat_tools(calls, text)
    if family == APIFamily.ANTHROPIC_MESSAGES:
        return anthropic_tools(calls, text)
    return responses_tools(calls, text)


def profile(
    family: APIFamily,
    *,
    model: str = "fixture",
    base_url: str = "https://fixture.invalid",
    **values: Any,
) -> ModelProfile:
    """Build a profile whose endpoint cannot be reached without MockTransport."""
    return ModelProfile(api_family=family, model=model, base_url=base_url, **values)


@dataclass
class MockModelTransport:
    """Sequential SSE responses plus every decoded request body.

    The transport is intentionally strict about consumption.  A test that sends
    an unexpected extra request fails instead of hanging or silently reusing the
    last response.  ``close`` is handled by the caller's ``async with`` client;
    this object has no independent network client.
    """

    responses: list[str]
    requests: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    response_status: int = 200

    @classmethod
    def from_responses(cls, responses: Iterable[str]) -> MockModelTransport:
        return cls(list(responses), [], [])

    def handler(self, request):
        import httpx

        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": request.content,
            }
        )
        self.requests.append(json.loads(request.content))
        if not self.responses:
            raise AssertionError("unexpected extra model request")
        return httpx.Response(
            self.response_status,
            headers={"content-type": "text/event-stream"},
            text=self.responses.pop(0),
        )

    def remaining(self) -> int:
        return len(self.responses)


