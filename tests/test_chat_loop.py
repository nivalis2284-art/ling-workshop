"""Acceptance tests for the unified chat loop (C phase).

One chat turn is a bounded multi-step loop: the model may request tools, the
loop runs each call through ``BEFORE_TOOL → dispatch → AFTER_TOOL``, feeds the
native results back, and repeats until the model stops asking or the step budget
runs out. Two structural properties are proven here:

* the loop itself is honest — every model call is accounted per step, and an
  unfinished run is reported as unfinished, never as completed;
* policy is load-bearing *through hooks* — permission and side-effect
  confirmation are enforced by ``BEFORE_TOOL`` handlers, so removing the hook is
  observably what stops the block (the loop body never holds that decision).

These are deterministic: no network, no real model, no container engine.
"""

import asyncio
import json
import re

import httpx
import pytest

from lingnexus.common.errors import NotFound
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.permissions.hooks import AppAuthorization, permission_hook
from lingnexus.runtime.confirmations import ConfirmationService, confirmation_hook
from lingnexus.runtime.hooks import HookEvent, HookRegistry
from lingnexus.runtime.types import ChatRequest

# --- shared builders (shape matches tests/test_smoke.py) -------------------


def profile(*, context_window_tokens=None, max_output_tokens=None):
    return ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
    )


def configure(app, **limits):
    item = app.profiles.save("fixture", profile(**limits), api_key="runtime-secret-only")
    app.profiles.select(item["id"], None)
    return item["id"]


def reply(text):
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def tool_reply(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
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
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def mock_client(responses, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        value = responses.pop(0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=value)

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


def _grant(app, agent_id, folder):
    folder.mkdir(exist_ok=True)
    app.permissions.configure(
        agent_id, readable=[folder], writable=[folder], expected_revision=0
    )


# --- the loop really does the work, step by step --------------------------


def test_chat_loop_writes_reads_and_finishes_in_one_turn(app, agent_id, tmp_path):
    configure(app)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    target = folder / "answer.txt"
    requests = []
    replies = [
        tool_reply(
            "先写文件。",
            [("write-id", "create_file", {"path": str(target), "text": "actual artifact"})],
        ),
        tool_reply(calls=[("read-id", "read_file", {"path": str(target)})]),
        reply("已写入并核对 answer.txt。"),
    ]
    result, events = run_chat(
        app,
        replies,
        {"agent_id": agent_id, "message": "创建并核对 answer.txt", "request_id": "loop-one"},
        requests,
    )
    assert result["status"] == "completed"
    assert target.read_text() == "actual artifact"

    steps = [event["data"]["step"] for event in events if event["event"] == "step_started"]
    assert steps == [1, 2, 3]
    started = [
        (event["data"]["step"], event["data"]["name"])
        for event in events if event["event"] == "tool_started"
    ]
    assert started == [(1, "create_file"), (2, "read_file")]
    results = [
        (event["data"]["step"], event["data"]["name"], event["data"]["status"])
        for event in events if event["event"] == "tool_result"
    ]
    assert [(step, name) for step, name, _ in results] == [(1, "create_file"), (2, "read_file")]
    assert all(status == "completed" for _, _, status in results)
    assert any(event["event"] == "complete" for event in events)

    # The native tool results travelled back to the model on the next step.
    assert requests[2]["messages"][-1]["tool_call_id"] == "read-id"
    assert "actual artifact" in json.dumps(requests[2], ensure_ascii=False)


def test_every_step_is_accounted_separately(app, agent_id, tmp_path):
    configure(app)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    target = folder / "answer.txt"
    replies = [
        tool_reply(
            calls=[("w", "create_file", {"path": str(target), "text": "x"})],
        ),
        tool_reply(calls=[("r", "read_file", {"path": str(target)})]),
        reply("完成"),
    ]
    result, _ = run_chat(
        app,
        replies,
        {"agent_id": agent_id, "message": "多步", "request_id": "loop-account"},
    )
    assert result["status"] == "completed"
    with app.database.transaction() as db:
        rows = db.execute(
            "SELECT step,attempt,status FROM chat_call_attempts WHERE run_id=? ORDER BY step,attempt",
            (result["id"],),
        ).fetchall()
    # One row per model call, not one run-level row.
    assert [(row["step"], row["attempt"]) for row in rows] == [(1, 1), (2, 1), (3, 1)]
    assert all(row["status"] == "completed" for row in rows)


def test_exhausted_step_budget_is_reported_not_hidden(app, agent_id, tmp_path):
    configure(app)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    target = folder / "loop.txt"
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("chat.max_steps", json.dumps(1)),
        )
    # A model that always asks for a tool can never finish within one step.
    replies = [
        tool_reply(calls=[("w", "create_file", {"path": str(target), "text": "x"})]),
    ]
    result, events = run_chat(
        app,
        replies,
        {"agent_id": agent_id, "message": "永远不满足", "request_id": "loop-budget"},
    )
    assert result["status"] != "completed"
    notices = [event for event in events if event["event"] == "notice"]
    assert any(item["data"]["code"] == "step_budget_exhausted" for item in notices)
    assert any(event["event"] == "error" for event in events)
    assert not any(event["event"] == "complete" for event in events)


# --- policy is load-bearing through hooks ---------------------------------


def test_permission_is_enforced_by_the_hook_not_the_loop(app, agent_id, tmp_path):
    """Removing the hook is exactly what stops the block.

    With the assembled ``Application`` the out-of-scope write is rejected; the
    same payload against an empty registry is allowed — proving the decision
    lives in the ``BEFORE_TOOL`` handler, not in any loop body.
    """
    outside = tmp_path / "outside" / "secret.txt"
    payload = {
        "name": "write_file",
        "arguments": {"path": str(outside), "text": "nope"},
        "context": {"agent_id": agent_id, "command_available": False},
    }

    denied = asyncio.run(app.hooks.run(HookEvent.BEFORE_TOOL, payload))
    assert denied.allow is False
    assert denied.reason

    allowed = asyncio.run(HookRegistry().run(HookEvent.BEFORE_TOOL, payload))
    assert allowed.allow is True

    # A bare registry carrying only the permission hook behaves like the app.
    registry = HookRegistry()
    registry.register(HookEvent.BEFORE_TOOL, permission_hook(AppAuthorization(app)))
    again = asyncio.run(registry.run(HookEvent.BEFORE_TOOL, payload))
    assert again.allow is False


def test_search_is_authorized_by_readable_grants_never_as_a_path(app, agent_id, tmp_path):
    """``search`` has no ``path`` argument; a hook that checks it like
    ``read_file`` would deny it every single time (the file tools then starve
    the model of any way to find files). The gate is the readable grant.
    """
    configure(app)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    (folder / "notes.txt").write_text("预算一千七百六十", encoding="utf-8")

    replies = [
        tool_reply(calls=[("s", "search", {"query": "预算", "match": "content"})]),
        reply("找到了。"),
    ]
    result, events = run_chat(
        app,
        replies,
        {"agent_id": agent_id, "message": "找预算", "request_id": "loop-search"},
    )
    assert result["status"] == "completed"
    tool_result = next(event for event in events if event["event"] == "tool_result")
    assert tool_result["data"]["status"] == "completed", tool_result["data"]["preview"]

    # An agent with no readable grant is still refused, for its own reason.
    other = app.create_agent("乙")
    hook = permission_hook(AppAuthorization(app))
    denied = asyncio.run(
        hook(
            {
                "name": "search",
                "arguments": {"query": "预算"},
                "context": {"agent_id": other, "command_available": False},
            }
        )
    )
    assert denied is not None and denied.allow is False
    assert "尚未授权" in (denied.reason or "")


def test_confirmation_gate_allows_non_gated_tools_and_denies_until_approved():
    service = ConfirmationService(timeout_seconds=5)
    handler = confirmation_hook(service)

    # Non-gated tools never wait: the hook passes immediately.
    assert asyncio.run(
        handler({"name": "read_file", "arguments": {}, "context": {"run_id": "r1"}})
    ) is None

    arguments = {"argv": ["echo", "hi"]}
    fingerprint = ConfirmationService.fingerprint("run_command", arguments)
    payload = {"name": "run_command", "arguments": arguments, "context": {"run_id": "r1"}}

    async def reject_then_approve():
        first = asyncio.create_task(handler(payload))
        await asyncio.sleep(0.01)
        assert not first.done()
        assert [item["tool"] for item in service.pending("r1")] == ["run_command"]
        service.decide("r1", fingerprint, approved=False)
        denied = await first
        assert denied is not None and denied.allow is False
        assert denied.reason

        second = asyncio.create_task(handler(payload))
        await asyncio.sleep(0.01)
        service.decide("r1", fingerprint, approved=True)
        approved = await second
        assert approved is None

    asyncio.run(reject_then_approve())


def test_confirmation_times_out_to_a_refusal():
    service = ConfirmationService(timeout_seconds=0.01)
    handler = confirmation_hook(service)
    result = asyncio.run(
        handler(
            {
                "name": "run_command",
                "arguments": {"argv": ["echo", "hi"]},
                "context": {"run_id": "r-timeout"},
            }
        )
    )
    assert result is not None and result.allow is False


def test_fingerprint_is_stable_and_argument_sensitive():
    a = ConfirmationService.fingerprint("run_command", {"argv": ["ls"], "cwd": "/tmp"})
    b = ConfirmationService.fingerprint("run_command", {"cwd": "/tmp", "argv": ["ls"]})
    c = ConfirmationService.fingerprint("run_command", {"argv": ["rm"], "cwd": "/tmp"})
    assert a == b
    assert a != c


def test_deciding_an_unknown_confirmation_is_not_found():
    service = ConfirmationService()
    with pytest.raises(NotFound):
        service.decide("r1", "missing", approved=True)


# --- the confirmation surface is reachable over the local API -------------


def test_confirmation_routes_are_registered(client, headers):
    # This FastAPI version lazy-includes routers, so app.routes does not flatten
    # them; the OpenAPI schema is the authoritative route table.
    paths = client.get("/openapi.json", headers=headers).json()["paths"]
    assert "get" in paths["/chat/runs/{run_id}/confirmations"]
    assert "post" in paths["/chat/runs/{run_id}/confirmations/{fingerprint}"]
    # The route reaches its handler: an unknown run is a 404 from the run store,
    # which a route miss could not distinguish on its own.
    assert client.get("/chat/runs/nope/confirmations", headers=headers).status_code == 404


# --- the opening carries facts, the tail carries time -----------------------


def test_opening_states_the_granted_environment(app, agent_id, tmp_path):
    configure(app)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    requests = []
    result, _ = run_chat(
        app,
        [reply("好")],
        {"agent_id": agent_id, "message": "你好", "request_id": "env-1"},
        requests,
    )
    assert result["status"] == "completed"
    system = requests[0]["messages"][0]["content"]
    assert "运行环境：" in system
    assert "- 平台：" in system
    assert str(folder) in system
    assert "命令执行：可用" in system
    # The opening must never carry a moving clock; time belongs to the message tail.
    assert not re.search(r"\d{4}-\d{2}-\d{2}", system)


def test_opening_reports_unavailable_commands_without_grants(app, agent_id):
    configure(app)
    requests = []
    run_chat(
        app,
        [reply("好")],
        {"agent_id": agent_id, "message": "你好", "request_id": "env-2"},
        requests,
    )
    system = requests[0]["messages"][0]["content"]
    assert "- 可写目录：无" in system
    assert "- 可读目录：无" in system
    assert "命令执行：不可用" in system
