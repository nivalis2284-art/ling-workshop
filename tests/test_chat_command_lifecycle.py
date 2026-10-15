import asyncio
import json
import sys

import httpx

from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRuntime
from lingnexus.runtime.types import ChatRequest


def _frame(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {"index": i, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (call_id, name, args) in enumerate(calls)
        ]
    event = {"id": "chat-session-lifecycle", "choices": [{
        "index": 0, "delta": delta,
        "finish_reason": "tool_calls" if calls else "stop",
    }]}
    return "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"


def _configure(app, agent_id, folder):
    profile = app.profiles.save(
        "command-lifecycle", ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="offline-lifecycle", base_url="https://fixture.invalid/v1",
        ), api_key="fixture-only",
    )
    app.profiles.select(profile["id"], None)
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
    conversation = app.conversations.create(agent_id, "command lifecycle")
    app.access.set(conversation["id"], "full")
    return conversation["id"]


def _command(script):
    return ("cmd", "run_command", {
        "argv": [sys.executable, "-c", script], "purpose": "work", "long_running": True,
    })


def test_chat_final_after_running_command_fails_and_prevents_delayed_write(app, agent_id, tmp_path):
    async def scenario():
        folder = tmp_path / "final-command"
        conversation_id = _configure(app, agent_id, folder)
        script = (
            "import pathlib,time; time.sleep(5); "
            "pathlib.Path('late.txt').write_text('must not appear')"
        )
        responses = [
            _frame("Starting a long command.", [_command(script)]),
            _frame("Done."),
        ]

        def handler(_request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=responses.pop(0)
            )

        runtime = ChatRuntime(app, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            run = runtime.submit(ChatRequest(
                agent_id=agent_id, message="Run the command", request_id="final-running-command",
                conversation_id=conversation_id,
            ))
            for _ in range(800):
                value = runtime.get_run(run["id"])
                if value["status"] != "running":
                    break
                await asyncio.sleep(0.02)
            assert value["status"] == "failed", value
            assert value["recoverable"] is False
            assert value["recovery_reason"] is not None
            error = next(event for event in runtime.store.read_events(run["id"], 0)[0]
                         if event["event"] == "error")
            assert error["data"]["code"] in {"command_session_active", "command_session_unknown"}
            records = app.sandbox.run_sessions(agent_id, run["id"], "chat")
            assert records and records[0]["status"] in {"cancelled", "unknown"}
            await asyncio.sleep(5.2)
            assert not (folder / "late.txt").exists()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_chat_owner_cancel_waits_for_owned_command_cleanup(app, agent_id, tmp_path):
    async def scenario():
        folder = tmp_path / "cancel-command"
        conversation_id = _configure(app, agent_id, folder)
        script = (
            "import pathlib,time; time.sleep(5); "
            "pathlib.Path('late.txt').write_text('must not appear')"
        )
        first_request = True

        async def handler(_request):
            nonlocal first_request
            if first_request:
                first_request = False
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    text=_frame("Starting a long command.", [_command(script)]),
                )
            await asyncio.Event().wait()

        runtime = ChatRuntime(app, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            run = runtime.submit(ChatRequest(
                agent_id=agent_id, message="Run then cancel", request_id="cancel-running-command",
                conversation_id=conversation_id,
            ))
            records = []
            for _ in range(300):
                records = app.sandbox.run_sessions(agent_id, run["id"], "chat")
                if records and records[0]["status"] in {"running", "cancelling"}:
                    break
                await asyncio.sleep(0.02)
            assert records and records[0]["status"] in {"running", "cancelling"}
            value = await runtime.cancel(run["id"])
            assert value["status"] == "cancelled", value
            records = app.sandbox.run_sessions(agent_id, run["id"], "chat")
            assert records[0]["status"] == "cancelled", records
            owner = app.sandbox._owner(agent_id, run["id"], "chat")
            result = app.sandbox.sessions.poll(records[0]["session_id"], owner)["result"]
            assert result["cleanup_confirmed"] is True
            await asyncio.sleep(5.2)
            assert not (folder / "late.txt").exists()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
