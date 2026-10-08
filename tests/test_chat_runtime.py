"""Chat contract exercised through real services and httpx's external boundary.

All credentials and streams here are fictional. No test contacts a live model.
"""

import asyncio
import json
from contextlib import aclosing, asynccontextmanager

import httpx
import pytest

from lingnexus.common.types import SourceRef
from lingnexus.models import ModelCapabilities, ModelProfile

KEY = "fixture-chat-key-not-real"
OPAQUE = "opaque-private-reasoning-never-persist"
FAMILIES = ["anthropic_messages", "openai_chat_completions", "openai_responses"]


def wire(*items):
    return "".join(
        "data: " + (item if isinstance(item, str) else json.dumps(item)) + "\n\n" for item in items
    ).encode()


def chunk(delta=None, finish=None):
    return {
        "id": "chat_fixture",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }


def success(family, answer="Hello 灵", *, finish_reason=None):
    if family == "openai_chat_completions":
        return wire(
            chunk({"role": "assistant", "reasoning_content": "Visible summary"}),
            chunk({"content": answer, "opaque_signature": OPAQUE}),
            chunk(finish=finish_reason or "stop"),
            {
                "id": "chat_fixture",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "native_private_field": OPAQUE,
                },
            },
            "[DONE]",
        )
    if family == "anthropic_messages":
        return wire(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": "fixture-model",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Visible summary"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": OPAQUE},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": answer},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": finish_reason or "end_turn"},
                "usage": {"output_tokens": 4, "native_private_field": OPAQUE},
            },
            {"type": "message_stop"},
        )
    reasoning = {
        "id": "rs_fixture",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": OPAQUE,
    }
    message = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }
    return wire(
        {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "item_id": "rs_fixture",
            "summary_index": 0,
            "delta": "Visible summary",
        },
        {"type": "response.output_item.added", "output_index": 1, "item": message},
        {
            "type": "response.output_text.delta",
            "output_index": 1,
            "item_id": "msg_fixture",
            "content_index": 0,
            "delta": answer,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_fixture",
                "status": "completed",
                "output": [
                    reasoning,
                    {
                        **message,
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": answer, "annotations": []},
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                    "native_private_field": OPAQUE,
                },
            },
        },
    )


def configure(app, family="openai_chat_completions", *, key=KEY, **overrides):
    profile = ModelProfile(
        api_family=family,
        model="fixture-model",
        base_url="https://fixture.invalid",
        capabilities=ModelCapabilities(effort_levels=("low", "high")),
        max_output_tokens=128,
        **overrides,
    )
    item = app.profiles.save("Fixture", profile, api_key=key)
    app.profiles.select(item["id"], None)
    return item["id"]


def request(agent_id, **overrides):
    from lingnexus.runtime import ChatRequest

    return ChatRequest(agent_id=agent_id, message="Hi", request_id="turn-1", **overrides)


def source_rows(app, agent_id):
    with app.database.transaction() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT s.kind,r.content FROM sources s JOIN source_revisions r "
                "ON s.id=r.source_id AND s.current_revision=r.revision "
                "WHERE s.agent_id=? ORDER BY s.rowid",
                (agent_id,),
            )
        ]


@pytest.mark.parametrize("family", FAMILIES)
def test_native_payload_normalized_events_and_successful_visible_history(app, agent_id, family):
    from lingnexus.runtime import ChatRuntime

    profile_id = configure(app, family)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=success(family)
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id, effort="low"))
                assert run["status"] == "running"
                assert source_rows(app, agent_id) == [{"kind": "user", "content": "Hi"}]
                events = [event async for event in runtime.events(run["id"])]
                assert events[0] == {
                    "id": 1,
                    "event": "started",
                    "data": {"run_id": run["id"], "conversation_id": run["conversation_id"]},
                }
                assert [e["id"] for e in events] == list(range(1, len(events) + 1))
                assert [e["data"]["text"] for e in events if e["event"] == "text"] == ["Hello 灵"]
                assert [e["data"]["text"] for e in events if e["event"] == "thinking"] == [
                    "Visible summary",
                ]
                assert events[-1]["event"] == "complete"
                assert events[-1]["data"]["status"] == "completed"
                ref = SourceRef(**events[-1]["data"]["assistant_source"])
                assert app.history.read(ref)["text"] == "Hello 灵"
                usage = [e["data"]["usage"] for e in events if e["event"] == "usage"][-1]
                assert (usage["input_tokens"], usage["output_tokens"]) == (10, 4)
                assert "raw" not in usage
                assert runtime.get_run(run["id"])["profile_id"] == profile_id
                assert runtime.get_run(run["id"])["status"] == "completed"
            finally:
                await runtime.aclose()
            assert not client.is_closed

    asyncio.run(scenario())
    assert len(calls) == 1

    body = json.loads(calls[0].content)
    assert body["model"] == "fixture-model" and body["stream"] is True
    assert "tools" not in body
    if family == "openai_responses":
        assert body["input"] == [{"role": "user", "content": "Hi"}]
        assert "messages" not in body
        assert body["instructions"]
        assert body["reasoning"]["effort"] == "low"
        assert calls[0].url.path == "/v1/responses"
    elif family == "anthropic_messages":
        assert body["messages"] == [{"role": "user", "content": "Hi"}]
        assert body["system"] and body["max_tokens"] == 128
        assert body["output_config"]["effort"] == "low"
        assert calls[0].headers["x-api-key"] == KEY
        assert calls[0].url.path == "/v1/messages"
    else:
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1:] == [{"role": "user", "content": "Hi"}]
        assert body["reasoning_effort"] == "low"
        assert calls[0].url.path == "/v1/chat/completions"
    if family != "anthropic_messages":
        assert calls[0].headers["authorization"] == "Bearer " + KEY
    assert source_rows(app, agent_id) == [
        {"kind": "user", "content": "Hi"},
        {"kind": "assistant", "content": "Hello 灵"},
    ]
    with app.database.transaction() as db:
        dump = "\n".join(db.iterdump())
    assert KEY not in dump and OPAQUE not in dump


@pytest.mark.parametrize(
    ("family", "finish_reason", "code"),
    [
        ("openai_chat_completions", "length", "incomplete_response"),
        ("openai_chat_completions", "content_filter", "incomplete_response"),
        ("openai_chat_completions", OPAQUE, "incomplete_response"),
        ("openai_chat_completions", "end_turn", "incomplete_response"),
        ("openai_chat_completions", "completed", "incomplete_response"),
        ("anthropic_messages", "max_tokens", "incomplete_response"),
        ("anthropic_messages", "pause_turn", "incomplete_response"),
        ("anthropic_messages", OPAQUE, "incomplete_response"),
        ("anthropic_messages", "stop", "incomplete_response"),
        ("anthropic_messages", "completed", "incomplete_response"),
        ("openai_chat_completions", "tool_calls", "tools_unsupported"),
        ("openai_chat_completions", "function_call", "tools_unsupported"),
        ("anthropic_messages", "tool_use", "tools_unsupported"),
    ],
)
def test_non_success_finish_keeps_visible_events_without_assistant_source(
    app, agent_id, family, finish_reason, code
):
    from lingnexus.runtime import ChatRuntime

    configure(app, family)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=success(family, "Partial answer", finish_reason=finish_reason),
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id))
                events = [e async for e in runtime.events(run["id"])]
                assert events[-1]["event"] == "error"
                assert events[-1]["data"]["code"] == code
                assert not any(e["event"] == "complete" for e in events)
                assert [e["data"]["text"] for e in events if e["event"] == "text"] == [
                    "Partial answer"
                ]
                assert [e["data"]["text"] for e in events if e["event"] == "thinking"] == [
                    "Visible summary"
                ]
                usage = [e["data"]["usage"] for e in events if e["event"] == "usage"][-1]
                assert (usage["input_tokens"], usage["output_tokens"]) == (10, 4)
                assert runtime.get_run(run["id"])["status"] == "failed"
                assert runtime.get_run(run["id"])["assistant_source"] is None
                assert source_rows(app, agent_id) == [{"kind": "user", "content": "Hi"}]
                assert [e async for e in runtime.events(run["id"])] == events
                assert OPAQUE not in json.dumps(events)
            finally:
                await runtime.aclose()

    asyncio.run(scenario())


def test_anthropic_stop_sequence_is_a_successful_finish(app, agent_id):
    from lingnexus.runtime import ChatRuntime

    configure(app, "anthropic_messages")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=success("anthropic_messages", finish_reason="stop_sequence"),
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id))
                events = [e async for e in runtime.events(run["id"])]
                assert events[-1]["event"] == "complete"
                ref = SourceRef(**events[-1]["data"]["assistant_source"])
                assert app.history.read(ref)["text"] == "Hello 灵"
                assert runtime.get_run(run["id"])["status"] == "completed"
            finally:
                await runtime.aclose()

    asyncio.run(scenario())


@pytest.fixture
def chat_api(tmp_path):
    from fastapi.testclient import TestClient

    from lingnexus.api.app import create_app
    from lingnexus.api.chat import router
    from lingnexus.runtime import ChatRuntime

    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=success("openai_chat_completions"),
        )

    server = create_app(
        data_dir=tmp_path, token="fixture-owner-token", frontend_dir=tmp_path / "no-frontend"
    )
    # Parent owns composition-root integration. Mount our public router if that
    # parallel change is not yet present, and retain real auth/error handlers.
    if not any(getattr(route, "path", None) == "/chat/stream" for route in server.routes):
        server.include_router(router)
        server.include_router(router, prefix="/api")
    original_lifespan = server.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            if existing := getattr(app.state, "chat_runtime", None):
                await existing.aclose()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as external:
                runtime = ChatRuntime(app.state.services, client=external)
                app.state.chat_runtime = runtime
                try:
                    yield
                finally:
                    await runtime.aclose()

    server.router.lifespan_context = lifespan
    with TestClient(server, base_url="http://localhost:8765") as client:
        yield client, calls


def owner():
    return {"Authorization": "Bearer fixture-owner-token"}


def decode_events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    records = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        records.append(
            {"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])}
        )
    return records


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_api_stream_auth_persistence_replay_and_reconnect(chat_api, prefix):
    client, calls = chat_api
    app = client.app.state.services
    agent = app.create_agent("Fixture")
    configure(app)
    value = {"agent_id": agent, "message": "Hi", "request_id": "api-turn"}
    url = prefix + "/chat/stream"
    assert client.post(url, json=value).status_code == 401
    assert (
        client.post(
            url, headers=owner() | {"Origin": "https://untrusted.invalid"}, json=value
        ).status_code
        == 403
    )
    assert app.history.count(agent) == 0 and calls == []
    response = client.post(url, headers=owner(), json=value)
    events = decode_events(response)
    run_id = events[0]["data"]["run_id"]
    assert events[0] == {
        "id": 1,
        "event": "started",
        "data": {"run_id": run_id, "conversation_id": app.conversations.list(agent)[0]["id"]},
    }
    assert response.headers["cache-control"] == "no-cache"
    assert events[-1]["event"] == "complete"
    source = client.get(
        "/sources/" + events[-1]["data"]["assistant_source"]["id"],
        params={"revision": 1},
        headers=owner(),
    ).json()
    assert source["kind"] == "assistant" and source["text"] == "Hello 灵"
    assert app.history.count(agent) == 2
    app.profiles.clear_credentials()
    assert decode_events(client.post(url, headers=owner(), json=value)) == events
    assert len(calls) == 1
    assert client.post(url, headers=owner(), json=value | {"message": "Changed"}).status_code == 409
    events_url = prefix + f"/chat/runs/{run_id}/events"
    cancel_url = prefix + f"/chat/runs/{run_id}/cancel"
    status_url = prefix + f"/chat/runs/{run_id}"
    for method, path in [
        (client.get, events_url),
        (client.post, cancel_url),
        (client.get, status_url),
    ]:
        assert method(path).status_code == 401
        assert (
            method(path, headers=owner() | {"Origin": "https://untrusted.invalid"}).status_code
            == 403
        )
    status = client.get(status_url, headers=owner())
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["assistant_source"] == events[-1]["data"]["assistant_source"]
    assert status.json()["context_sources"] == [status.json()["user_source"]]
    assert KEY not in status.text and "base_url" not in status.text
    assert decode_events(client.get(events_url, params={"after": 1}, headers=owner())) == events[1:]
    assert (
        decode_events(client.get(events_url, params={"after": events[-1]["id"]}, headers=owner()))
        == []
    )
    assert client.get(events_url, params={"after": -1}, headers=owner()).status_code == 422
    assert client.post(cancel_url, headers=owner()).json()["status"] == "completed"
    assert client.get(prefix + "/chat/runs/missing/events", headers=owner()).status_code == 404
    assert client.post(prefix + "/chat/runs/missing/cancel", headers=owner()).status_code == 404
    assert client.get(prefix + "/chat/runs/missing", headers=owner()).status_code == 404
    assert KEY not in response.text and OPAQUE not in response.text


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("after", [-1, 2**63, 10**100, 2**63 - 1])
def test_api_event_cursor_bounds_are_validated_before_streaming(chat_api, prefix, after):
    client, calls = chat_api
    app = client.app.state.services
    agent = app.create_agent("Fixture")
    configure(app)
    events = decode_events(
        client.post(
            prefix + "/chat/stream",
            headers=owner(),
            json={"agent_id": agent, "message": "Hi", "request_id": "cursor"},
        )
    )
    run_id = events[0]["data"]["run_id"]
    result = client.get(
        prefix + f"/chat/runs/{run_id}/events", params={"after": after}, headers=owner()
    )
    if after == 2**63 - 1:
        assert decode_events(result) == []
    else:
        assert result.status_code == 422
        assert result.headers["content-type"].startswith("application/json")
        assert "detail" in result.json()
    assert len(calls) == 1


@pytest.mark.parametrize("layer", ["runtime", "store"])
@pytest.mark.parametrize("after", [-1, 2**63, 10**100, 2**63 - 1])
def test_event_cursor_bounds_are_enforced_without_http(app, agent_id, layer, after):
    from lingnexus.common.errors import DomainError
    from lingnexus.runtime import ChatRuntime

    configure(app)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=success("openai_chat_completions"),
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id))
                events = [e async for e in runtime.events(run["id"])]

                async def read():
                    if layer == "runtime":
                        return [e async for e in runtime.events(run["id"], after=after)]
                    return runtime.store.read_events(run["id"], after)[0]

                if after == 2**63 - 1:
                    assert await read() == []
                else:
                    with pytest.raises(DomainError):
                        await read()
                assert [e async for e in runtime.events(run["id"])] == events
                assert runtime.get_run(run["id"])["status"] == "completed"
            finally:
                await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "extra", [{"tools": [{"type": "function"}]}, {"tool_choice": "auto"}, {"functions": []}]
)
def test_api_explicitly_rejects_tools_before_recording_a_user_source(chat_api, extra):
    client, calls = chat_api
    app = client.app.state.services
    agent = app.create_agent("Fixture")
    configure(app)
    result = client.post(
        "/chat/stream",
        headers=owner(),
        json={
            "agent_id": agent,
            "message": "Hi",
            "request_id": "tools",
            **extra,
        },
    )
    assert result.status_code == 422
    assert "Tools are not supported" in result.json()["detail"]
    assert app.history.count(agent) == 0 and calls == []


def test_api_missing_credentials_and_extra_fields_are_safe_preflight_errors(chat_api):
    client, calls = chat_api
    app = client.app.state.services
    agent = app.create_agent("Fixture")
    profile_id = configure(app, key=None)
    value = {
        "agent_id": agent,
        "message": "Hi",
        "request_id": "missing-key",
        "profile_id": profile_id,
    }
    assert client.post("/chat/stream", headers=owner(), json=value).status_code == 403
    invalid = client.post("/chat/stream", headers=owner(), json=value | {"api_key": KEY})
    assert invalid.status_code == 422 and KEY not in invalid.text
    assert app.history.count(agent) == 0 and calls == []


@pytest.mark.parametrize("failure", ["eof", "http", "provider", "transport", "tool", "exception"])
def test_failed_model_run_keeps_only_user_source_and_never_leaks_errors(app, agent_id, failure):
    from lingnexus.runtime import ChatRuntime

    configure(app)
    unsafe = KEY + OPAQUE

    def handler(req):
        if failure == "transport":
            raise httpx.ConnectError(unsafe)
        if failure == "exception":
            raise RuntimeError(unsafe)
        if failure == "http":
            return httpx.Response(401, text=unsafe)
        tail = {
            "eof": b"",
            "provider": wire({"error": {"message": unsafe, "type": "server_error"}}),
            "tool": wire(
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fixture",
                                "type": "function",
                                "function": {"name": "shell", "arguments": "{}"},
                            }
                        ]
                    }
                ),
                chunk(finish="tool_calls"),
                "[DONE]",
            ),
        }[failure]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=wire(chunk({"content": "Unfinished"})) + tail,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id))
                events = [e async for e in runtime.events(run["id"])]
                assert events[-1]["event"] == "error"
                assert "complete" not in [e["event"] for e in events]
                assert runtime.get_run(run["id"])["status"] == "failed"
                assert runtime.get_run(run["id"])["assistant_source"] is None
                assert KEY not in json.dumps(events) and OPAQUE not in json.dumps(events)
                assert source_rows(app, agent_id) == [{"kind": "user", "content": "Hi"}]
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    with app.database.transaction() as db:
        assert unsafe not in "\n".join(db.iterdump())


@pytest.mark.parametrize("before_started", [False, True])
def test_explicit_cancel_is_terminal_without_assistant_source(app, agent_id, before_started):
    from lingnexus.runtime import ChatRuntime

    configure(app)

    async def scenario():
        stream = GatedStream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=stream,
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id))
                if not before_started:
                    await asyncio.wait_for(stream.waiting.wait(), 2)
                assert runtime.cancel(run["id"])["status"] == "cancelled"
                assert runtime.cancel(run["id"])["status"] == "cancelled"
                events = [e async for e in runtime.events(run["id"])]
                assert events[-1]["event"] == "error"
                assert events[-1]["data"]["code"] == "cancelled"
                assert sum(e["event"] == "error" for e in events) == 1
                assert app.history.count(agent_id) == 1
            finally:
                await runtime.aclose()
            if not before_started:
                assert stream.closed

    asyncio.run(scenario())


def test_shutdown_and_restart_interrupt_unfinished_runs_without_reissuing(
    app, agent_id, app_factory
):
    from lingnexus.runtime import ChatRuntime

    configure(app)

    async def scenario():
        stream = GatedStream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=stream,
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(request(agent_id))
            await asyncio.wait_for(stream.waiting.wait(), 2)
            await runtime.aclose()
            assert stream.closed
            assert runtime.get_run(run["id"])["status"] == "interrupted"
            # Simulate the durable state left by a killed process (no shutdown hook).
            with app.database.transaction() as db:
                db.execute("DELETE FROM chat_events WHERE run_id=? AND event='error'", (run["id"],))
                db.execute("UPDATE chat_runs SET status='running' WHERE id=?", (run["id"],))
            reopened = app_factory(app.database.path)
            recovered = ChatRuntime(reopened, client=client)
            try:
                assert recovered.submit(request(agent_id))["id"] == run["id"]
                events = [e async for e in recovered.events(run["id"])]
                assert events[-1]["data"]["code"] == "interrupted"
                assert recovered.get_run(run["id"])["status"] == "interrupted"
                assert reopened.history.count(agent_id) == 1
            finally:
                await recovered.aclose()
                reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem", ["credentials", "selection", "profile", "effort", "agent", "budget"]
)
def test_rejected_submission_does_not_save_sources_or_call_provider(app, agent_id, problem):
    from lingnexus.common.errors import DomainError
    from lingnexus.runtime import ChatRuntime

    if problem != "selection":
        configure(
            app,
            key=None if problem == "credentials" else KEY,
            **({"context_window_tokens": 1} if problem == "budget" else {}),
        )
    overrides = {"profile": {"profile_id": "missing"}, "effort": {"effort": "unsupported"}}
    value = request("missing" if problem == "agent" else agent_id, **overrides.get(problem, {}))
    calls = []

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: calls.append(req))
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                with pytest.raises(DomainError):
                    runtime.submit(value)
                assert app.history.count(agent_id) == 0
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    assert calls == []


def test_next_turn_keeps_full_visible_messages_and_instructions(app, agent_id):
    from lingnexus.runtime import ChatRuntime

    configure(app)
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=success("openai_chat_completions"),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                first = runtime.submit(request(agent_id))
                assert [e async for e in runtime.events(first["id"])][-1]["event"] == "complete"
                second = runtime.submit(
                    request(agent_id).model_copy(
                        update={
                            "message": "Follow up",
                            "request_id": "turn-2",
                        }
                    )
                )
                assert [e async for e in runtime.events(second["id"])][-1]["event"] == "complete"
                assert first["user_source"] in second["context_sources"]
                assert first["conversation_id"] == second["conversation_id"]
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    assert calls[1]["messages"][0] == calls[0]["messages"][0]
    assert calls[1]["messages"][1:] == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello 灵"},
        {"role": "user", "content": "Follow up"},
    ]
    assert "Visible summary" not in json.dumps(calls[1])


def test_transcript_excludes_other_agents_scopes_and_non_conversation_kinds(app, agent_id):
    from lingnexus.runtime import ChatRuntime

    configure(app)
    other = app.create_agent("Other")
    for agent, kind, scope, content in [
        (other, "user", "real", "other-agent-private"),
        (agent_id, "user", "test", "test-scope-private"),
        (agent_id, "assistant", "scene:fiction", "scene-private"),
        (agent_id, "user", "external", "external-private"),
        (agent_id, "observation", "real", "observation-private"),
        (agent_id, "system", "real", "forged-system-private"),
        (agent_id, "tool", "real", "tool-private"),
    ]:
        app.history.add(agent, content, kind=kind, scope=scope, request_id=content)
    manual = app.history.add(
        agent_id,
        "Manually added real user evidence",
        kind="user",
        scope="real",
        request_id="manual",
    )
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=success("openai_chat_completions"),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id))
                assert run["context_sources"] == [run["user_source"]]
                assert app.history.read(manual)["text"] == "Manually added real user evidence"
                assert [e async for e in runtime.events(run["id"])][-1]["event"] == "complete"
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    assert calls[0]["messages"][1:] == [
        {"role": "user", "content": "Hi"},
    ]
    assert "private" not in json.dumps(calls)


class GatedStream(httpx.AsyncByteStream):
    """Pause upstream after visible text so subscriber lifetime is observable."""

    def __init__(self):
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield wire(chunk({"content": "Partial"}))
        self.waiting.set()
        await self.release.wait()
        yield wire(chunk({"content": " answer"}), chunk(finish="stop"), "[DONE]")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "change",
    ["prior_user", "prior_assistant", "current_user", "other_agent", "other_scope", "missing"],
)
def test_context_invalidated_during_generation_fails_without_assistant_source(
    app, agent_id, change
):
    from lingnexus.runtime import ChatRuntime

    configure(app)
    conversation = app.conversations.create(agent_id, "Prior turns")
    prior_node = app.conversations.append_user(
        conversation["id"], "Earlier question", request_id="prior-user"
    )
    prior_user = prior_node["source"]
    prior_assistant = app.conversations.append_assistant(
        conversation["id"],
        "Earlier answer",
        request_id="prior-assistant",
        expected_head=prior_node["id"],
    )["source"]
    other = app.create_agent("Other")

    async def scenario():
        stream = GatedStream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id, conversation_id=conversation["id"]))
                assert run["context_sources"] == [
                    prior_user.model_dump(),
                    prior_assistant.model_dump(),
                    run["user_source"],
                ]
                await asyncio.wait_for(stream.waiting.wait(), 2)
                if change in ("prior_user", "prior_assistant", "current_user"):
                    target = {
                        "prior_user": prior_user,
                        "prior_assistant": prior_assistant,
                        "current_user": SourceRef(**run["user_source"]),
                    }[change]
                    app.history.correct(target, "Corrected during generation")
                else:
                    with app.database.transaction() as db:
                        if change == "other_agent":
                            db.execute(
                                "UPDATE sources SET agent_id=? WHERE id=?", (other, prior_user.id)
                            )
                        elif change == "other_scope":
                            db.execute(
                                "UPDATE sources SET scope='test' WHERE id=?", (prior_user.id,)
                            )
                        else:
                            refs = [*run["context_sources"], {"id": "missing", "revision": 1}]
                            db.execute(
                                "UPDATE chat_runs SET context_sources=? WHERE id=?",
                                (json.dumps(refs), run["id"]),
                            )
                history_before = source_rows(app, agent_id)
                jobs_before = app.history.pending_jobs(agent_id)
                stream.release.set()
                events = [e async for e in runtime.events(run["id"])]
                assert events[-1]["event"] == "error"
                assert events[-1]["data"]["code"] == "context_invalidated"
                assert not any(e["event"] == "complete" for e in events)
                assert [e["data"]["text"] for e in events if e["event"] == "text"] == [
                    "Partial",
                    " answer",
                ]
                finished = runtime.get_run(run["id"])
                assert finished["status"] == "failed"
                assert finished["assistant_source"] is None
                assert source_rows(app, agent_id) == history_before
                assert app.history.pending_jobs(agent_id) == jobs_before
                assert [e async for e in runtime.events(run["id"])] == events
            finally:
                await runtime.aclose()
            assert stream.closed

    asyncio.run(scenario())


def test_correcting_uncaptured_source_during_generation_does_not_invalidate_context(app, agent_id):
    from lingnexus.runtime import ChatRuntime

    configure(app)
    conversation = app.conversations.create(agent_id, "Captured branch")
    captured = app.conversations.append_user(
        conversation["id"], "Earlier question", request_id="prior-user"
    )["source"]
    uncaptured = app.history.add(
        agent_id, "Observation", kind="observation", scope="real", request_id="observation"
    )

    async def scenario():
        stream = GatedStream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request(agent_id, conversation_id=conversation["id"]))
                assert run["context_sources"] == [captured.model_dump(), run["user_source"]]
                await asyncio.wait_for(stream.waiting.wait(), 2)
                app.history.correct(uncaptured, "Corrected observation")
                stream.release.set()
                events = [e async for e in runtime.events(run["id"])]
                assert events[-1]["event"] == "complete"
                ref = SourceRef(**events[-1]["data"]["assistant_source"])
                assert app.history.read(ref)["text"] == "Partial answer"
            finally:
                await runtime.aclose()

    asyncio.run(scenario())


def test_detaching_subscriber_keeps_run_alive_and_replay_never_calls_model_twice(app, agent_id):
    from lingnexus.common.errors import Conflict
    from lingnexus.runtime import ChatRuntime

    configure(app)
    calls = []

    async def scenario():
        stream = GatedStream()

        def handler(req):
            calls.append(req)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                value = request(agent_id)
                run = runtime.submit(value)
                async with aclosing(runtime.events(run["id"])) as subscriber:
                    assert (await anext(subscriber))["event"] == "started"
                    delta = await anext(subscriber)
                    assert delta["data"]["text"] == "Partial"
                await asyncio.wait_for(stream.waiting.wait(), 2)
                assert runtime.get_run(run["id"])["status"] == "running"
                assert not stream.closed
                assert app.history.count(agent_id) == 1
                assert runtime.submit(value)["id"] == run["id"]
                with pytest.raises(Conflict):
                    runtime.submit(value.model_copy(update={"message": "Different"}))
                with pytest.raises(Conflict):
                    runtime.submit(value.model_copy(update={"request_id": "overlapping"}))
                stream.release.set()
                events = [e async for e in runtime.events(run["id"], after=delta["id"])]
                assert events[0]["data"]["text"] == " answer"
                assert events[-1]["event"] == "complete"
                assert events[-1]["data"]["status"] == "completed"
                app.profiles.clear_credentials()
                assert runtime.submit(value)["id"] == run["id"]
                assert [e async for e in runtime.events(run["id"], after=events[-1]["id"])] == []
                assert app.history.count(agent_id) == 2
            finally:
                await runtime.aclose()
            assert stream.closed

    asyncio.run(scenario())
    assert len(calls) == 1
