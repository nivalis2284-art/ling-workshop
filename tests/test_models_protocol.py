"""Native fixture SSE through the real HTTP boundary; no live model calls."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

FAKE_KEY = "fixture-key-not-a-real-credential"


class Packets(httpx.AsyncByteStream):
    def __init__(self, data, width=1, fail=False):
        self.data = data
        self.width = width
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        for i in range(0, len(self.data), self.width):
            yield self.data[i : i + self.width]
        if self.fail:
            raise httpx.ReadError(FAKE_KEY)

    async def aclose(self):
        self.closed = True


def sse(*events, newline="\n"):
    parts = []
    for event in events:
        if isinstance(event, str):
            parts.append(f"data: {event}{newline}{newline}")
        else:
            name = f"event: {event['type']}{newline}" if "type" in event else ""
            parts.append(name + "data: " + json.dumps(event, ensure_ascii=False) + newline * 2)
    return "".join(parts).encode()


def chat(delta=None, finish=None, **extra):
    return {
        "id": "chat_1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        **extra,
    }


def start_message():
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "fixture",
            "usage": {"input_tokens": 11, "output_tokens": 1},
            "native_extension": {"keep": True},
        },
    }


def block_start(index, block):
    return {"type": "content_block_start", "index": index, "content_block": block}


def block_delta(index, kind, **delta):
    return {"type": "content_block_delta", "index": index, "delta": {"type": kind, **delta}}


def block_stop(index):
    return {"type": "content_block_stop", "index": index}


def message_end(reason="end_turn"):
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]


def response_end(output=None, kind="completed", **extra):
    return {
        "type": f"response.{kind}",
        "response": {
            "id": "resp_1",
            "status": kind,
            "output": output or [],
            **extra,
        },
    }


def collect(
    family,
    data,
    *,
    payload=None,
    profile_options=None,
    gateway_options=None,
    status=200,
    content_type="text/event-stream",
    width=1,
    fail=False,
    effort=None,
):
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    packets = Packets(data, width, fail)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, stream=packets)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(client, **(gateway_options or {}))
            profile = ModelProfile(
                api_family=family,
                model="fixture-model",
                base_url="https://fixture.invalid",
                **(profile_options or {}),
            )
            request = (
                payload
                if payload is not None
                else (
                    {"input": "hello"}
                    if family == "openai_responses"
                    else {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
                )
            )
            result = [
                event
                async for event in gateway.stream(
                    profile,
                    request,
                    credentials=RuntimeCredentials(api_key=FAKE_KEY),
                    effort=effort,
                )
            ]
            assert not client.is_closed
            return result

    return asyncio.run(run()), requests, packets


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("width", [1, 7, 65536])
def test_chat_byte_packets_utf8_multiline_usage_and_terminal(newline, width):
    first = json.dumps(chat({"role": "assistant", "content": "你好🌍"}), ensure_ascii=False)
    first = first.replace(', "choices"', ',\n"choices"')
    wire = (
        "\ufeff: heartbeat"
        + newline * 2
        + newline.join("data: " + line for line in first.splitlines())
        + newline * 2
    ).encode()
    wire += sse(
        chat(finish="stop"),
        {
            "id": "chat_1",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 6,
                "total_tokens": 16,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        },
        "[DONE]",
        newline=newline,
    )
    events, requests, packets = collect("openai_chat_completions", wire, width=width)
    assert [e.type for e in events] == ["text_delta", "usage", "complete"]
    assert events[0].text == "你好🌍"
    assert events[1].usage.input_tokens == 10
    assert events[1].usage.reasoning_tokens == 2
    assert events[-1].continuation.response_id == "chat_1"
    assert events[-1].continuation.response["choices"][0]["message"]["content"] == "你好🌍"
    body = json.loads(requests[0].content)
    assert body["stream_options"]["include_usage"] is True
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert packets.closed


def test_chat_interleaved_tools_keep_ids_and_assemble_arguments():
    wire = sse(
        chat(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    },
                    {
                        "index": 1,
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "clock", "arguments": '{"zone":'},
                    },
                ]
            }
        ),
        chat(
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": '"香港"}'}},
                    {"index": 0, "function": {"arguments": '"東京"}'}},
                ]
            }
        ),
        chat(finish="tool_calls"),
        "[DONE]",
    )
    events, _, _ = collect("openai_chat_completions", wire)
    tools = [e.tool_call for e in events if e.type == "tool_call"]
    assert [(t.id, t.name, t.arguments) for t in tools] == [
        ("call_a", "weather", {"city": "東京"}),
        ("call_b", "clock", {"zone": "香港"}),
    ]
    fragments = [e for e in events if e.type == "tool_call_delta"]
    assert [e.tool_call_id for e in fragments] == ["call_a", "call_b", "call_b", "call_a"]
    assert (
        "".join(e.arguments_delta for e in fragments if e.tool_call_id == "call_a")
        == tools[0].arguments_json
    )
    assert events[-1].type == "complete"
    assert events[-1].usage.input_tokens is None
    assert events[-1].usage.cost_usd is None


def test_anthropic_thinking_signatures_redacted_blocks_tools_and_cumulative_usage():
    redacted = {"type": "redacted_thinking", "data": "opaque-do-not-display"}
    wire = sse(
        start_message(),
        block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        block_delta(0, "thinking_delta", thinking="Visible summary"),
        block_delta(0, "signature_delta", signature="signed-"),
        block_delta(0, "signature_delta", signature="原样"),
        block_stop(0),
        block_start(1, redacted),
        block_stop(1),
        block_start(2, {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}),
        block_delta(2, "input_json_delta", partial_json='{"term":'),
        block_delta(2, "input_json_delta", partial_json='"灵"}'),
        block_stop(2),
        *message_end("tool_use"),
    )
    events, requests, _ = collect("anthropic_messages", wire)
    assert [e.text for e in events if e.type == "thinking_delta"] == ["Visible summary"]
    tool = next(e.tool_call for e in events if e.type == "tool_call")
    assert (tool.id, tool.arguments) == ("toolu_1", {"term": "灵"})
    final = events[-1]
    assert final.type == "complete"
    assert (final.usage.input_tokens, final.usage.output_tokens) == (11, 7)
    assert final.continuation.response["content"][0]["signature"] == "signed-原样"
    assert final.continuation.response["content"][1] == redacted
    assert final.continuation.response["native_extension"] == {"keep": True}
    assert final.continuation.events[0] == start_message()
    assert requests[0].headers["x-api-key"] == FAKE_KEY
    assert requests[0].headers["anthropic-version"] == "2023-06-01"
    assert requests[0].url.path == "/v1/messages"


def test_responses_preserves_opaque_reasoning_and_distinct_item_and_call_ids():
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "opaque-final",
        "summary": [{"type": "summary_text", "text": "Visible"}],
        "provider_future_field": {"signature": "retain"},
    }
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"q":"灵"}',
        "status": "completed",
    }
    output = [reasoning, tool]
    wire = sse(
        {"type": "response.created", "response": {"id": "resp_1", "output": []}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**reasoning, "encrypted_content": "incomplete", "summary": []},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "delta": "Visible",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**tool, "arguments": "", "status": "in_progress"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "item_id": "fc_1",
            "delta": '{"q":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "item_id": "fc_1",
            "delta": '"灵"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 1,
            "item_id": "fc_1",
            "arguments": tool["arguments"],
        },
        {"type": "response.output_item.done", "output_index": 1, "item": tool},
        response_end(
            output,
            usage={
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 28,
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        ),
    )
    events, _, _ = collect("openai_responses", wire)
    assert [e.text for e in events if e.type == "thinking_delta"] == ["Visible"]
    calls = [e.tool_call for e in events if e.type == "tool_call"]
    assert len(calls) == 1
    assert (calls[0].id, calls[0].item_id, calls[0].arguments) == ("call_1", "fc_1", {"q": "灵"})
    assert events[-1].continuation.response["output"] == output
    assert events[-1].usage.reasoning_tokens == 5
    assert "opaque-final" not in repr(events)


@pytest.mark.parametrize(
    "family,wire,code",
    [
        ("openai_chat_completions", b"data: {bad}\n\n", "malformed_stream"),
        ("openai_chat_completions", b"data: []\n\n", "malformed_stream"),
        ("openai_chat_completions", b'data: {"bad":"\xff"}\n\n', "malformed_stream"),
        ("openai_chat_completions", sse(chat({"content": "partial"})), "unexpected_eof"),
        ("openai_chat_completions", sse(chat(finish="stop")), "unexpected_eof"),
        ("openai_chat_completions", sse("[DONE]"), "malformed_stream"),
        (
            "anthropic_messages",
            sse(start_message(), block_delta(4, "text_delta", text="oops")),
            "malformed_stream",
        ),
        (
            "openai_responses",
            sse({"type": "response.output_text.delta", "delta": 42}),
            "malformed_stream",
        ),
        (
            "anthropic_messages",
            sse({"type": "error", "error": {"type": "overloaded_error", "message": FAKE_KEY}}),
            "provider_error",
        ),
        ("openai_chat_completions", sse({"error": {"message": FAKE_KEY}}), "provider_error"),
        (
            "openai_responses",
            sse(response_end(kind="failed", error={"message": FAKE_KEY})),
            "provider_error",
        ),
        (
            "openai_responses",
            sse(
                response_end(kind="incomplete", incomplete_details={"reason": "max_output_tokens"})
            ),
            "incomplete_response",
        ),
    ],
)
def test_malformed_truncated_and_native_errors_never_complete(family, wire, code):
    events, _, packets = collect(family, wire)
    assert events[-1].type == "error"
    assert events[-1].error.code == code
    assert not any(e.type == "complete" for e in events)
    assert FAKE_KEY not in repr(events)
    assert FAKE_KEY not in events[-1].error.message
    assert packets.closed


def test_invalid_tool_json_is_error_not_executable_call():
    events, _, _ = collect(
        "openai_chat_completions",
        sse(
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bad", "arguments": '{"x":'},
                        }
                    ]
                }
            ),
            chat(finish="tool_calls"),
            "[DONE]",
        ),
    )
    assert events[-1].error.code == "invalid_tool_arguments"
    assert not any(e.type in ("complete", "tool_call") for e in events)


def test_http_and_transport_errors_do_not_expose_keys(caplog):
    events, _, packets = collect("openai_responses", FAKE_KEY.encode(), status=401)
    assert events[-1].error.code == "http_error"
    assert events[-1].error.status_code == 401
    assert packets.closed
    events2, _, _ = collect("openai_responses", b"", fail=True)
    assert events2[-1].error.code == "transport_error"
    assert FAKE_KEY not in repr(events + events2) + caplog.text


def test_wrong_content_type_and_stream_limits():
    from lingnexus.models import StreamLimits

    events, _, _ = collect("openai_responses", b"{}", content_type="application/json")
    assert events[-1].error.code == "content_type"
    events, _, _ = collect(
        "openai_responses",
        b"data: " + b"x" * 100,
        gateway_options={"limits": StreamLimits(max_event_bytes=32)},
    )
    assert events[-1].error.code == "stream_limit"
    events, _, _ = collect(
        "anthropic_messages",
        sse(*[{"type": "ping"}] * 8),
        gateway_options={"limits": StreamLimits(max_stream_bytes=60)},
    )
    assert events[-1].error.code == "stream_limit"


@pytest.mark.parametrize(
    "family,base,expected",
    [
        ("openai_responses", "https://example.test/", "https://example.test/v1/responses"),
        ("openai_responses", "https://example.test/v1/", "https://example.test/v1/responses"),
        (
            "anthropic_messages",
            "https://example.test/v1/messages/",
            "https://example.test/v1/messages",
        ),
        (
            "openai_chat_completions",
            "https://example.test/proxy/v1/",
            "https://example.test/proxy/v1/chat/completions",
        ),
        (
            "openai_responses",
            "https://example.test/custom/",
            "https://example.test/custom/responses",
        ),
    ],
)
def test_profile_endpoint_normalization_and_unknown_metadata(family, base, expected):
    from lingnexus.models import ModelProfile

    profile = ModelProfile(api_family=family, model="explicit-model", base_url=base)
    assert profile.endpoint_url == expected
    assert profile.context_window_tokens is None
    assert profile.max_output_tokens is None
    assert profile.input_price_per_million is None
    assert profile.output_price_per_million is None
    assert "api_key" not in profile.model_dump()


@pytest.mark.parametrize(
    "base",
    [
        "ftp://example.test",
        "https://user:secret@example.test",
        "https://example.test?api_key=secret",
        "https://example.test/#secret",
    ],
)
def test_profile_rejects_credentials_or_ambiguous_base_url(base):
    from lingnexus.models import ModelProfile

    with pytest.raises(ValueError):
        ModelProfile(api_family="openai_responses", model="fixture", base_url=base)


@pytest.mark.parametrize(
    "family,field",
    [
        ("anthropic_messages", "output_config"),
        ("openai_chat_completions", "reasoning_effort"),
        ("openai_responses", "reasoning"),
    ],
)
def test_capability_effort_mapping_preserves_native_request(family, field):
    from lingnexus.models import ModelCapabilities

    payload = {"messages": [], "max_tokens": 64, "native_extension": {"keep": [1]}}
    if family == "openai_responses":
        payload = {"input": [], "reasoning": {"summary": "auto"}, "store": False}
    before = deepcopy(payload)
    events, requests, _ = collect(
        family,
        b"",
        payload=payload,
        effort="high",
        profile_options={
            "capabilities": ModelCapabilities(thinking=True, effort_levels=("low", "high"))
        },
    )
    body = json.loads(requests[0].content)
    assert body[field] == (
        "high" if field == "reasoning_effort" else {**before.get(field, {}), "effort": "high"}
    )
    assert body["model"] == "fixture-model"
    assert body["stream"] is True
    assert payload == before
    assert events[-1].error.code == "unexpected_eof"


def test_unsupported_capability_is_rejected_before_http_and_credentials_repr_is_safe():
    from lingnexus.models import ModelCapabilities, RuntimeCredentials

    credentials = RuntimeCredentials(api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(credentials)
    events, requests, _ = collect("openai_responses", b"", effort="high")
    assert events[-1].error.code == "invalid_request"
    assert not requests
    events, requests, _ = collect(
        "openai_responses",
        b"",
        payload={"input": [], "tools": [{}]},
        profile_options={"capabilities": ModelCapabilities(tools=False)},
    )
    assert events[-1].error.code == "invalid_request"
    assert not requests


def test_response_text_refusal_unknown_events_and_continuation_request_are_lossless():
    from lingnexus.models import ModelCapabilities

    native = {
        "id": "rs_old",
        "type": "reasoning",
        "encrypted_content": "preserve-me",
        "summary": [],
    }
    payload = {"input": [native], "include": ["reasoning.encrypted_content"], "store": False}
    unknown = {"type": "response.future_event", "opaque": {"value": 8}}
    events, requests, _ = collect(
        "openai_responses",
        sse(
            unknown,
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "msg_1", "type": "message", "status": "in_progress"},
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "Hello",
            },
            {
                "type": "response.refusal.delta",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 1,
                "delta": "Cannot do that",
            },
            response_end(),
        ),
        payload=payload,
        profile_options={"capabilities": ModelCapabilities(encrypted_reasoning=True)},
    )
    assert [e.text for e in events if e.type == "text_delta"] == ["Hello", "Cannot do that"]
    assert events[-1].continuation.events[0] == unknown
    assert json.loads(requests[0].content)["input"] == [native]
    assert events[-1].usage.input_tokens is None


@pytest.mark.parametrize(
    "payload",
    [
        {"input": [], "include": ["reasoning.encrypted_content"]},
        {"input": [], "reasoning": {"summary": "auto"}},
        {"input": [], "reasoning": {"effort": "high"}},
    ],
)
def test_native_requests_cannot_bypass_capability_flags(payload):
    events, requests, _ = collect("openai_responses", b"", payload=payload)
    assert events[-1].error.code == "invalid_request"
    assert not requests


def test_anthropic_tool_arguments_keep_original_whitespace():
    arguments = '{ "city" : "東京" }'
    wire = sse(
        start_message(),
        block_start(0, {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {}}),
        block_delta(0, "input_json_delta", partial_json=arguments),
        block_stop(0),
        *message_end("tool_use"),
    )
    events, _, _ = collect("anthropic_messages", wire)
    assert next(e.tool_call for e in events if e.type == "tool_call").arguments_json == arguments


@pytest.mark.parametrize(
    "final_change", [{"id": "fc_wrong"}, {"call_id": "call_wrong"}, {"arguments": '{"q":2}'}]
)
def test_responses_rejects_conflicting_tool_completion(final_change):
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": "",
        "status": "in_progress",
    }
    wire = sse(
        {"type": "response.output_item.added", "output_index": 0, "item": tool},
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_1",
            "delta": '{"q":1}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {**tool, "arguments": '{"q":1}', "status": "completed", **final_change},
        },
        response_end(),
    )
    events, _, _ = collect("openai_responses", wire)
    assert events[-1].error.code == "malformed_stream"
    assert not any(e.type == "tool_call" for e in events)


def test_invalid_anthropic_citation_shape_is_a_normalized_error():
    events, _, _ = collect(
        "anthropic_messages",
        sse(
            start_message(),
            block_start(0, {"type": "text", "text": "", "citations": 42}),
            block_delta(0, "citations_delta", citation={"type": "char_location"}),
        ),
    )
    assert events[-1].error.code == "malformed_stream"


def test_redirects_are_not_followed_even_with_client_default_enabled():
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.invalid"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            gateway = ModelGateway(client)
            profile = ModelProfile(
                api_family="openai_responses", model="fixture", base_url="https://fixture.invalid"
            )
            return [
                e
                async for e in gateway.stream(
                    profile, {"input": []}, credentials=RuntimeCredentials(FAKE_KEY)
                )
            ]

    events = asyncio.run(run())
    assert len(seen) == 1
    assert events[-1].error.status_code == 307


def test_explicit_iterator_close_releases_response_without_closing_client():
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    packets = Packets(sse(chat({"content": "hello"}), chat(finish="stop"), "[DONE]"))

    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=packets
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = ModelGateway(client)
            profile = ModelProfile(
                api_family="openai_chat_completions",
                model="fixture",
                base_url="https://fixture.invalid",
            )
            events = gateway.stream(
                profile, {"messages": []}, credentials=RuntimeCredentials(FAKE_KEY)
            )
            assert (await anext(events)).text == "hello"
            await events.aclose()
            assert packets.closed
            assert not client.is_closed

    asyncio.run(run())


def test_deadline_does_not_cancel_consumer_between_stream_reads():
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    packets = Packets(sse(chat({"content": "hello"}), chat(finish="stop"), "[DONE]"))

    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=packets
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = ModelGateway(client, total_timeout_seconds=0.03)
            profile = ModelProfile(
                api_family="openai_chat_completions",
                model="fixture",
                base_url="https://fixture.invalid",
            )
            events = gateway.stream(
                profile, {"messages": []}, credentials=RuntimeCredentials(FAKE_KEY)
            )
            try:
                assert (await anext(events)).text == "hello"
                await asyncio.sleep(0.05)
                assert (await anext(events)).error.code == "timeout"
            finally:
                await events.aclose()
            assert packets.closed

    asyncio.run(run())


def test_terminal_event_already_closed_the_http_response():
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    packets = Packets(sse(response_end()))

    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=packets
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            profile = ModelProfile(
                api_family="openai_responses", model="fixture", base_url="https://fixture.invalid"
            )
            events = ModelGateway(client).stream(
                profile, {"input": []}, credentials=RuntimeCredentials(FAKE_KEY)
            )
            try:
                assert (await anext(events)).type == "complete"
                assert packets.closed
            finally:
                await events.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [True, False])
def test_blocked_transport_cancellation_and_deadline_release_response(cancel):
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    async def run():
        entered = asyncio.Event()

        class BlockingPackets(Packets):
            async def __aiter__(self):
                yield b": waiting\n\n"
                entered.set()
                await asyncio.Event().wait()

        packets = BlockingPackets(b"")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=packets
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = ModelGateway(client, total_timeout_seconds=5 if cancel else 0.03)
            profile = ModelProfile(
                api_family="openai_responses", model="fixture", base_url="https://fixture.invalid"
            )
            events = gateway.stream(
                profile, {"input": []}, credentials=RuntimeCredentials(FAKE_KEY)
            )
            task = asyncio.create_task(anext(events))
            await entered.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert (await task).error.code == "timeout"
            await events.aclose()
            assert packets.closed
            assert not client.is_closed

    asyncio.run(run())


def test_shared_gateway_keeps_concurrent_credentials_and_streams_separate():
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    async def run():
        async def handler(request):
            model = json.loads(request.content)["model"]
            assert request.headers["authorization"] == f"Bearer fixture-{model}"
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Packets(sse(response_end(id=f"resp_{model}"))),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(client)

            async def call(model):
                profile = ModelProfile(
                    api_family="openai_responses", model=model, base_url="https://fixture.invalid"
                )
                return [
                    e
                    async for e in gateway.stream(
                        profile, {"input": []}, credentials=RuntimeCredentials(f"fixture-{model}")
                    )
                ]

            first, second = await asyncio.gather(call("a"), call("b"))
            assert first[-1].continuation.response_id == "resp_a"
            assert second[-1].continuation.response_id == "resp_b"
            assert "fixture-a" not in repr(gateway) + repr(first)

    asyncio.run(run())


@pytest.mark.parametrize(
    "family", ["anthropic_messages", "openai_chat_completions", "openai_responses"]
)
def test_shared_client_authentication_headers_are_isolated_per_call(family):
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    inherited_auth = {
        "Authorization": "Bearer fixture-inherited",
        "Proxy-Authorization": "Basic fixture-proxy",
        "X-API-Key": "fixture-anthropic",
        "Api-Key": "fixture-azure",
        "X-Goog-Api-Key": "fixture-google",
        "X-Auth-Token": "fixture-token",
        "X-Litellm-Api-Key": "fixture-litellm",
        "X-Portkey-Api-Key": "fixture-portkey",
        "Cookie": "session=fixture-session",
    }
    benign = {
        "User-Agent": "fixture-agent",
        "Accept-Encoding": "identity",
        "X-Request-ID": "fixture-trace",
        "Anthropic-Beta": "fixture-beta",
        "OpenAI-Organization": "fixture-organization",
    }
    wire = {
        "anthropic_messages": sse(start_message(), *message_end()),
        "openai_chat_completions": sse(chat(finish="stop"), "[DONE]"),
        "openai_responses": sse(response_end()),
    }[family]
    requests = []

    async def run():
        async def handler(request):
            requests.append(request)
            assert client.headers == original_headers
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Packets(wire),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={**inherited_auth, **benign},
            auth=httpx.BasicAuth("fixture-user", "fixture-password"),
        ) as client:
            original_headers = client.headers.copy()
            original_auth = client.auth
            gateway = ModelGateway(client)

            async def call(model):
                profile = ModelProfile(
                    api_family=family, model=model, base_url="https://fixture.invalid"
                )
                payload = (
                    {"input": []}
                    if family == "openai_responses"
                    else {"messages": [], "max_tokens": 10}
                )
                return [
                    event
                    async for event in gateway.stream(
                        profile, payload, credentials=RuntimeCredentials(f"fixture-{model}")
                    )
                ]

            results = await asyncio.gather(call("a"), call("b"))
            assert all(events[-1].type == "complete" for events in results)
            assert client.headers == original_headers
            assert client.auth is original_auth
            assert not client.is_closed

    asyncio.run(run())
    assert len(requests) == 2
    for request in requests:
        model = json.loads(request.content)["model"]
        auth_header = "x-api-key" if family == "anthropic_messages" else "authorization"
        assert request.headers[auth_header] == (
            f"fixture-{model}" if family == "anthropic_messages" else f"Bearer fixture-{model}"
        )
        for header in inherited_auth:
            if header.lower() != auth_header:
                assert header not in request.headers
        for header, value in benign.items():
            assert request.headers[header] == value
        assert request.headers["accept"] == "text/event-stream"


@pytest.mark.parametrize(
    ("family", "endpoint"),
    [
        ("anthropic_messages", "messages"),
        ("openai_chat_completions", "chat/completions"),
        ("openai_responses", "responses"),
    ],
)
def test_shared_client_query_params_do_not_leak_across_providers(family, endpoint):
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    inherited_params = {
        "api_key": "fixture-A",
        "access_token": "fixture-A-token",
        "custom_option": "fixture-A-option",
    }
    wire = {
        "anthropic_messages": sse(start_message(), *message_end()),
        "openai_chat_completions": sse(chat(finish="stop"), "[DONE]"),
        "openai_responses": sse(response_end()),
    }[family]
    requests = []
    hooked_urls = []
    packets = []

    async def run():
        async def on_request(request):
            hooked_urls.append(str(request.url))
            assert client.params is original_params
            assert dict(client.params) == inherited_params

        async def handler(request):
            requests.append(request)
            assert client.params is original_params
            assert dict(client.params) == inherited_params
            await asyncio.sleep(0)
            assert dict(client.params) == inherited_params
            stream = Packets(wire)
            packets.append(stream)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://provider-a.invalid/client-prefix/",
            params=inherited_params,
            headers={"User-Agent": "fixture-agent", "X-Request-ID": "fixture-trace"},
            event_hooks={"request": [on_request]},
            timeout=23,
        ) as client:
            original_params = client.params
            gateway = ModelGateway(client, timeout_seconds=9)

            async def call(provider):
                profile = ModelProfile(
                    api_family=family,
                    model=f"fixture-{provider}",
                    base_url=f"https://provider-{provider}.invalid/proxy/v1",
                )
                payload = (
                    {"input": []}
                    if family == "openai_responses"
                    else {"messages": [], "max_tokens": 10}
                )
                return [
                    event
                    async for event in gateway.stream(
                        profile, payload, credentials=RuntimeCredentials(f"fixture-{provider}")
                    )
                ]

            results = await asyncio.gather(call("b"), call("c"))
            for events in results:
                assert [e.type for e in events if e.type in ("complete", "error")] == [
                    "complete"
                ]
                assert events[-1].type == "complete"
            assert client.params is original_params
            assert dict(client.params) == inherited_params
            assert client.timeout == httpx.Timeout(23)
            assert not client.is_closed
            assert len(packets) == 2
            assert all(stream.closed for stream in packets)

    asyncio.run(run())
    expected_urls = [
        f"https://provider-b.invalid/proxy/v1/{endpoint}",
        f"https://provider-c.invalid/proxy/v1/{endpoint}",
    ]
    assert sorted(str(request.url) for request in requests) == expected_urls
    assert sorted(hooked_urls) == expected_urls
    for request in requests:
        assert request.url.query == b""
        assert request.headers["user-agent"] == "fixture-agent"
        assert request.headers["x-request-id"] == "fixture-trace"
        assert request.headers["accept"] == "text/event-stream"
        assert request.extensions["timeout"] == httpx.Timeout(9).as_dict()


@pytest.mark.parametrize("observed", ["added", "arguments_done", "item_done"])
@pytest.mark.parametrize("final_output", ["missing", "message", "provider_tool", "moved"])
def test_responses_final_output_must_retain_observed_tool(observed, final_output):
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"x":1}',
        "status": "completed",
    }
    observed_events = [
        {
            "type": (
                "response.output_item.done" if observed == "item_done"
                else "response.output_item.added"
            ),
            "output_index": 0,
            "item": (
                tool if observed == "item_done"
                else {**tool, "status": "in_progress", "arguments": ""}
            ),
        }
    ]
    if observed == "arguments_done":
        observed_events.append(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fc_1",
                "arguments": tool["arguments"],
            }
        )
    message = {"id": "fc_1", "type": "message", "content": []}
    output = {
        "missing": [],
        "message": [message],
        "provider_tool": [{"id": "fc_1", "type": "web_search_call"}],
        "moved": [message, tool],
    }[final_output]
    final = response_end(output, opaque_signature="fixture-final-signature")
    events, _, packets = collect("openai_responses", sse(*observed_events, final))
    assert events[-1].type == "error"
    assert events[-1].error.code == "malformed_stream"
    assert not any(e.type == "complete" for e in events)
    assert events[-1].continuation.events == (*observed_events, final)
    assert events[-1].continuation.response == final["response"]
    assert packets.closed


@pytest.mark.parametrize("first_done", ["item", "completed_item", "arguments"])
@pytest.mark.parametrize("next_event", ["item_done", "arguments_done", "final", "delta", "added"])
def test_responses_finalized_arguments_cannot_change_without_deltas(first_done, next_event):
    # Omitted status is accepted as a completed tool by the existing interface.
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"x":1}',
    }
    if first_done == "completed_item":
        tool["status"] = "completed"
    if first_done != "arguments":
        prefix = [{"type": "response.output_item.done", "output_index": 0, "item": tool}]
    else:
        prefix = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**tool, "arguments": "", "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fc_1",
                "arguments": tool["arguments"],
            },
        ]
    changed = {**tool, "arguments": '{"x":2}'}
    if next_event == "delta":
        changed["arguments"] = tool["arguments"] + " "
        conflict = {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_1",
            "delta": " ",
        }
    elif next_event == "arguments_done":
        conflict = {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": "fc_1",
            "arguments": changed["arguments"],
        }
    elif next_event == "final":
        conflict = response_end([changed])
    else:
        conflict = {
            "type": (
                "response.output_item.added" if next_event == "added"
                else "response.output_item.done"
            ),
            "output_index": 0,
            "item": changed,
        }
    events, _, packets = collect(
        "openai_responses", sse(*prefix, conflict, response_end([changed]))
    )
    assert events[-1].type == "error"
    assert events[-1].error.code == "malformed_stream"
    assert not any(e.type == "complete" for e in events)
    assert events[-1].continuation.events[-1] == conflict
    assert packets.closed


def test_responses_consistent_repeated_completions_preserve_native_final_output():
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{ "x" : 1 }',
        "status": "completed",
    }
    args_done = {
        "type": "response.function_call_arguments.done",
        "output_index": 0,
        "item_id": "fc_1",
        "arguments": tool["arguments"],
    }
    item_done = {"type": "response.output_item.done", "output_index": 0, "item": tool}
    output = [
        {**tool, "opaque_signature": {"future": ["preserve"]}},
        {**tool, "id": "fc_2", "call_id": "call_2", "arguments": '{"x":2}'},
    ]
    wire_events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**tool, "arguments": "", "status": "in_progress"},
        },
        args_done,
        args_done,
        item_done,
        item_done,
        args_done,
        response_end(output),
    ]
    events, _, _ = collect("openai_responses", sse(*wire_events))
    assert events[-1].type == "complete"
    calls = [e.tool_call for e in events if e.type == "tool_call"]
    assert [(call.id, call.arguments_json) for call in calls] == [
        ("call_1", '{ "x" : 1 }'),
        ("call_2", '{"x":2}'),
    ]
    assert events[-1].continuation.response["output"] == output
    assert events[-1].continuation.events == tuple(wire_events)


def test_incomplete_responses_tool_is_not_emitted_as_complete_tool():
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"x":1}',
        "status": "incomplete",
    }
    events, _, _ = collect(
        "openai_responses",
        sse(
            {"type": "response.output_item.done", "output_index": 0, "item": tool},
            response_end([tool], kind="incomplete"),
        ),
    )
    assert events[-1].error.code == "incomplete_response"
    assert not any(e.type == "tool_call" for e in events)
    assert events[-1].continuation.response["output"] == [tool]


@pytest.mark.parametrize("item_event", ["added", "done"])
@pytest.mark.parametrize(
    "change",
    [
        {"id": "fc_wrong"},
        {"call_id": "different-call"},
        {"name": "different-tool"},
        {"arguments": '{"x":2}'},
    ],
)
def test_responses_final_response_cannot_change_previously_completed_tool(item_event, change):
    tool = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"x":1}',
        "status": "completed",
    }
    events, _, _ = collect(
        "openai_responses",
        sse(
            {"type": f"response.output_item.{item_event}", "output_index": 0, "item": tool},
            response_end([{**tool, **change}]),
        ),
    )
    assert events[-1].type == "error"
    assert events[-1].error.code == "malformed_stream"
    assert not any(e.type == "complete" for e in events)


def test_chat_returned_reasoning_content_is_visible_and_preserved_without_inference():
    delta = {
        "content": "Answer",
        "reasoning_content": "provider-extension",
        "opaque_signature": "keep-exactly",
    }
    events, _, _ = collect(
        "openai_chat_completions", sse(chat(delta), chat(finish="stop"), "[DONE]")
    )
    assert [e.text for e in events if e.type == "text_delta"] == ["Answer"]
    assert [e.text for e in events if e.type == "thinking_delta"] == ["provider-extension"]
    assert events[-1].continuation.events[0]["choices"][0]["delta"] == delta


def test_event_count_bound():
    from lingnexus.models import StreamLimits

    events, _, packets = collect(
        "anthropic_messages",
        sse(*[{"type": "ping"}] * 3),
        gateway_options={"limits": StreamLimits(max_events=2)},
    )
    assert events[-1].error.code == "stream_limit"
    assert packets.closed


def test_chat_reasoning_only_length_stop_preserves_inconsistent_reported_usage():
    usage = {"prompt_tokens": 10, "completion_tokens": 164, "total_tokens": 233}
    events, _, _ = collect(
        "openai_chat_completions",
        sse(
            chat({"role": "assistant", "reasoning_content": "Visible "}),
            chat({"reasoning_content": "returned material"}),
            chat(finish="length"),
            {"id": "chat_1", "choices": [], "usage": usage},
            "[DONE]",
        ),
    )
    assert [e.text for e in events if e.type == "thinking_delta"] == [
        "Visible ",
        "returned material",
    ]
    assert not any(e.type == "text_delta" for e in events)
    final = events[-1]
    assert (final.type, final.finish_reason) == ("complete", "length")
    assert (final.usage.input_tokens, final.usage.output_tokens, final.usage.total_tokens) == (
        10,
        164,
        233,
    )
    assert final.usage.raw == usage
    assert final.usage.cost_usd is None
    assert (
        final.continuation.response["choices"][0]["message"]["reasoning_content"]
        == "Visible returned material"
    )


def test_anthropic_latest_usage_keeps_nested_billing_without_recalculation():
    latest = {
        "input_tokens": 24,
        "output_tokens": 128,
        "billing_usage": {"input_tokens": 90, "output_tokens": 230, "native_detail": [1, 2]},
    }
    events, _, _ = collect(
        "anthropic_messages",
        sse(
            start_message(),
            block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
            block_delta(0, "thinking_delta", thinking="Visible"),
            block_delta(0, "signature_delta", signature="opaque-signature"),
            block_stop(0),
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": latest},
            {"type": "message_stop"},
        ),
    )
    final = events[-1]
    assert final.type == "complete"
    assert final.finish_reason == "max_tokens"
    assert (final.usage.input_tokens, final.usage.output_tokens) == (24, 128)
    assert final.usage.total_tokens is None
    assert final.usage.raw == latest
    assert final.continuation.response["usage"] == latest


@pytest.mark.parametrize("explicit_id", [False, True])
@pytest.mark.parametrize(
    "delta_type",
    [
        "response.output_text.delta",
        "response.refusal.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    ],
)
def test_responses_display_delta_resolves_only_established_item_id(delta_type, explicit_id):
    reasoning = "reasoning" in delta_type
    item_id = "rs_generated-id" if reasoning else "msg_generated-id"
    first = {"id": "rs_previous-id", "type": "reasoning", "summary": []}
    item = {"id": item_id, "type": "reasoning" if reasoning else "message", "status": "in_progress"}
    delta = {"type": delta_type, "output_index": 1, "delta": "OK"}
    delta["summary_index" if "summary" in delta_type else "content_index"] = 0
    if explicit_id:
        delta["item_id"] = item_id
    content_events = []
    done_events = []
    if delta_type == "response.output_text.delta":
        content_events = [
            {
                "type": "response.content_part.added",
                "output_index": 1,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            }
        ]
        done_events = [
            {
                "type": "response.output_text.done",
                "output_index": 1,
                "content_index": 0,
                "text": "OK",
            },
            {
                "type": "response.content_part.done",
                "output_index": 1,
                "content_index": 0,
                "part": {"type": "output_text", "text": "OK"},
            },
        ]
    finished_item = {**item, "status": "completed"}
    reported_usage = {"input_tokens": 10, "output_tokens": 164, "total_tokens": 233}
    wire_events = [
        {"type": "response.output_item.added", "output_index": 0, "item": first},
        {"type": "response.output_item.added", "output_index": 1, "item": item},
        *content_events,
        delta,
        *done_events,
        {"type": "response.output_item.done", "output_index": 1, "item": finished_item},
        response_end([first, finished_item], usage=reported_usage),
    ]
    events, _, packets = collect("openai_responses", sse(*wire_events))
    assert events[-1].type == "complete"
    displayed = [e for e in events if e.type in ("text_delta", "thinking_delta")]
    assert len(displayed) == 1
    assert (displayed[0].type, displayed[0].text, displayed[0].index, displayed[0].item_id) == (
        "thinking_delta" if reasoning else "text_delta",
        "OK",
        1,
        item_id,
    )
    assert events[-1].continuation.events == tuple(wire_events)
    assert events[-1].usage.total_tokens == 233
    assert events[-1].usage.raw == reported_usage
    assert events[-1].usage.cost_usd is None
    assert packets.closed


@pytest.mark.parametrize(
    "identity",
    [
        {"output_index": 1, "item_id": "msg_wrong-id"},
        {"output_index": 1, "item_id": None},
        {"output_index": 1, "item_id": ""},
        {"output_index": 1, "item_id": 7},
        {"item_id": "msg_generated-id"},
        {"output_index": 2},
        {"output_index": 2, "item_id": "msg_generated-id"},
    ],
)
def test_responses_display_delta_rejects_unestablished_or_conflicting_identity(identity):
    events, _, _ = collect(
        "openai_responses",
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"id": "msg_generated-id", "type": "message", "status": "in_progress"},
            },
            {"type": "response.output_text.delta", "content_index": 0, "delta": "OK", **identity},
            response_end(),
        ),
    )
    assert events[-1].type == "error"
    assert events[-1].error.code == "malformed_stream"
    assert not any(e.type in ("text_delta", "complete") for e in events)
