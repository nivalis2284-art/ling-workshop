"""F-2 acceptance: the MCP client is optional, and it grants nothing by itself.

Two failure modes matter most and both are covered here: a machine with nothing
configured must behave exactly as before, and a remote tool must be reached
through the same registry, the same dispatcher and the same hooks as a local one
— never around them.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from lingnexus.actions.dispatch import ToolDispatcher
from lingnexus.mcp import (
    ServerConfig,
    parse_servers,
    remote_tool_name,
    split_remote_tool,
)
from lingnexus.mcp.client import _parse_body
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime.hooks import HookEvent
from lingnexus.runtime.types import ChatRequest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


# --- helpers ---------------------------------------------------------------


def configure(app, servers):
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO settings VALUES ('mcp.servers',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"servers": servers}),),
        )
    app.mcp.reload()


def configure_model(app):
    """Register and select a model profile — a turn needs one before it runs."""
    item = app.profiles.save("fixture", profile(), api_key="runtime-secret-only")
    app.profiles.select(item["id"], None)
    return item["id"]


def stdio_server(name="fixture", *, version=None):
    entry = {"name": name, "transport": "stdio", "command": sys.executable, "args": [str(FIXTURE)]}
    if version is not None:
        entry["env"] = {"MCP_FIXTURE_VERSION": version}
    return entry


def profile():
    return ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
    )


def reply(text):
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def tool_reply(calls, text=None):
    delta = {"role": "assistant", "content": text}
    delta["tool_calls"] = [
        {
            "index": i,
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        for i, (cid, name, args) in enumerate(calls)
    ]
    frame = {
        "id": "chat-native",
        "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls"}],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def mock_client(responses, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=responses.pop(0)
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_chat(app, responses, request, requests=None):
    from lingnexus.runtime import ChatRuntime

    async def scenario():
        async with mock_client(responses, requests) as client:
            runtime = ChatRuntime(app, client=client)
            value = request if isinstance(request, ChatRequest) else ChatRequest(**request)
            run = runtime.submit(value)
            events = [event async for event in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            await runtime.aclose()
            return result, events

    return asyncio.run(scenario())


# --- optional: nothing configured means nothing happens -------------------


def test_unconfigured_service_opens_nothing_and_offers_nothing(app):
    assert app.mcp.configured() is False
    assert app.mcp.servers() == []
    assert app.mcp.specs() == []
    assert app.mcp.failures == {}


def test_dispatcher_declares_no_remote_tool_when_unconfigured(app):
    dispatcher = ToolDispatcher(mcp=app.mcp)

    names = [item["name"] for item in dispatcher.schema()]
    assert names == [spec.name for spec in dispatcher.registry.specs()]
    assert not any(name.startswith("mcp__") for name in names)


def test_configured_but_broken_server_does_not_fail_a_turn(app, agent_id):
    """A bad server contributes no tools; the conversation still completes."""
    configure(app, [{"name": "ghost", "transport": "stdio", "command": "definitely-not-a-command"}])
    configure_model(app)

    assert app.mcp.specs() == []
    assert "ghost" in app.mcp.failures

    run, events = run_chat(
        app,
        [reply("照常回答")],
        {"agent_id": agent_id, "message": "你好", "request_id": "r1"},
    )

    assert run["status"] == "completed"
    assert any(event["event"] == "text" for event in events)


# --- configuration parsing -------------------------------------------------


def test_malformed_entries_are_skipped_never_fatal():
    servers = parse_servers(
        [
            {"name": "ok", "transport": "http", "url": "https://example.test/mcp"},
            {"name": "", "transport": "http", "url": "https://x.test"},
            {"name": "bad__name", "transport": "http", "url": "https://x.test"},
            {"name": "no-url", "transport": "http"},
            {"name": "no-command", "transport": "stdio"},
            {"name": "off", "transport": "http", "url": "https://x.test", "enabled": False},
            {"name": "ok", "transport": "http", "url": "https://dup.test"},
            "not a dict",
        ]
    )

    assert [item.name for item in servers] == ["ok"]
    assert servers[0].url == "https://example.test/mcp"


def test_config_accepts_a_bare_list_and_rejects_junk():
    assert [item.name for item in parse_servers([{"name": "a", "url": "https://a.test"}])] == ["a"]
    assert parse_servers("nonsense") == []
    assert parse_servers(None) == []


def test_server_config_validates_transport():
    with pytest.raises(ValueError):
        ServerConfig({"name": "x", "transport": "carrier-pigeon"})
    with pytest.raises(ValueError):
        ServerConfig({"name": "a__b", "url": "https://x.test"})


def test_remote_names_cannot_collide_with_local_tools():
    from lingnexus.actions.catalog import registry

    declared = remote_tool_name("files", "read")
    assert declared == "mcp__files__read"
    assert split_remote_tool(declared) == ("files", "read")
    assert split_remote_tool("read_file") is None
    assert not any(name.startswith("mcp__") for name in registry.names())


# --- stdio: connect, list, call -------------------------------------------


def test_stdio_server_offers_its_tools(app):
    configure(app, [stdio_server()])

    specs = {spec.name: spec for spec in app.mcp.specs()}

    assert set(specs) == {"mcp__fixture__echo", "mcp__fixture__explode"}
    echo = specs["mcp__fixture__echo"]
    assert echo.kind == "mcp"
    # The server's own schema is passed through, not replaced by a local model.
    assert echo.input_schema["properties"]["text"]["type"] == "string"
    assert echo.schema()["parameters"] is echo.input_schema
    assert "[MCP:fixture]" in echo.description


def test_calling_a_remote_tool_returns_its_real_result(app):
    configure(app, [stdio_server()])
    app.mcp.specs()

    payload, effect = app.mcp.call("mcp__fixture__echo", {"text": "你好"})

    assert payload["status"] == "completed"
    assert payload["result"]["text"] == "echo:你好"
    assert payload["result"]["server"] == "fixture"
    assert effect is None


def test_a_tool_level_error_is_reported_not_swallowed(app):
    configure(app, [stdio_server()])
    app.mcp.specs()

    payload, _ = app.mcp.call("mcp__fixture__explode", {})

    assert payload["status"] == "failed"
    assert payload["error_code"] == "mcp_tool_error"
    assert payload["result"]["text"] == "boom"


def test_unknown_remote_tool_and_unknown_server_are_distinct(app):
    configure(app, [stdio_server()])
    app.mcp.specs()

    assert app.mcp.call("mcp__nope__x", {})[0]["error_code"] == "mcp_server_unavailable"
    assert app.mcp.call("not-a-remote-name", {})[0]["error_code"] == "mcp_unknown_tool"


# --- version negotiation is honest ----------------------------------------


def test_protocol_version_mismatch_is_reported_and_contributes_no_tools(app):
    configure(app, [stdio_server(version="1999-01-01")])

    assert app.mcp.specs() == []
    assert "1999-01-01" in app.mcp.failures["fixture"]

    payload, _ = app.mcp.call("mcp__fixture__echo", {"text": "x"})
    assert payload["status"] == "failed"
    assert payload["error_code"] == "mcp_server_unavailable"


def test_a_supported_older_revision_is_accepted(app):
    configure(app, [stdio_server(version="2024-11-05")])

    assert "mcp__fixture__echo" in {spec.name for spec in app.mcp.specs()}


# --- http transport -------------------------------------------------------


def test_event_stream_bodies_are_parsed():
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'

    assert _parse_body(body, "text/event-stream; charset=utf-8")["result"] == {"ok": True}


def test_plain_json_bodies_are_parsed():
    assert _parse_body(b'{"jsonrpc":"2.0","id":1,"result":{}}', "application/json")[
        "result"
    ] == {}


def test_http_server_is_reached(app):
    """A real HTTP round trip through the same client, using a stub server."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            message = json.loads(self.rfile.read(length) or b"{}")
            method = message.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub"},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": "ping", "description": "Ping.", "inputSchema": {}}]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "pong"}], "isError": False}
            else:
                result = {}
            body = json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
            payload = body.encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep the test output clean
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        configure(
            app,
            [{"name": "remote", "transport": "http", "url": f"http://127.0.0.1:{server.server_port}/mcp"}],
        )
        assert {spec.name for spec in app.mcp.specs()} == {"mcp__remote__ping"}
        payload, _ = app.mcp.call("mcp__remote__ping", {})
        assert payload["status"] == "completed"
        assert payload["result"]["text"] == "pong"
    finally:
        server.shutdown()
        server.server_close()


# --- the loop: same registry, same dispatcher, same hooks -----------------


def test_remote_tool_runs_through_the_loop_and_its_hooks(app, agent_id):
    configure(app, [stdio_server()])
    configure_model(app)
    seen: list[tuple[str, str | None]] = []

    async def spy(payload):
        seen.append((payload.get("name"), payload.get("status")))

    app.hooks.register(HookEvent.AFTER_TOOL, spy)
    requests: list[dict] = []

    run, events = run_chat(
        app,
        [
            tool_reply([("c1", "mcp__fixture__echo", {"text": "hi"})]),
            reply("收到远端回复"),
        ],
        {"agent_id": agent_id, "message": "调一下远端工具", "request_id": "r1"},
        requests=requests,
    )

    assert run["status"] == "completed"
    started = [event for event in events if event["event"] == "tool_started"]
    assert [event["data"]["name"] for event in started] == ["mcp__fixture__echo"]
    result = [event for event in events if event["event"] == "tool_result"]
    assert result and result[0]["data"]["status"] == "completed"
    assert "echo:hi" in result[0]["data"]["preview"]
    # The AFTER_TOOL hook saw the remote call — it is not a private path.
    assert seen == [("mcp__fixture__echo", "completed")]
    # The declaration actually sent to the model included the remote tool.
    declared = [tool["function"]["name"] for tool in requests[0]["tools"]]
    assert "mcp__fixture__echo" in declared
    # And the real result was fed back on the next turn.
    assert any("echo:hi" in json.dumps(message, ensure_ascii=False) for message in requests[1]["messages"])


def test_a_denying_hook_blocks_a_remote_tool(app, agent_id):
    """Policy still wins: the hooks run before any remote call is made."""
    configure(app, [stdio_server()])
    configure_model(app)

    async def refuse(payload):
        from lingnexus.runtime.hooks import HookResult

        if str(payload.get("name", "")).startswith("mcp__"):
            return HookResult.deny("所有者策略拒绝了该远端调用。")
        return None

    app.hooks.register(HookEvent.BEFORE_TOOL, refuse)

    run, events = run_chat(
        app,
        [tool_reply([("c1", "mcp__fixture__echo", {"text": "hi"})]), reply("好，不调了")],
        {"agent_id": agent_id, "message": "调远端", "request_id": "r1"},
    )

    assert run["status"] == "completed"
    result = next(event for event in events if event["event"] == "tool_result")
    assert result["data"]["status"] == "blocked"
    assert "所有者策略拒绝" in result["data"]["preview"]


# --- cleanup --------------------------------------------------------------


def test_close_stops_server_processes(app):
    configure(app, [stdio_server()])
    app.mcp.specs()
    connection = app.mcp._connections["fixture"]
    process = connection._transport._process

    app.mcp.close()

    assert process is not None
    assert process.poll() is not None
