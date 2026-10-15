"""Durable chat recovery regression tests driven through the real HTTP gateway."""

import asyncio
import json

import httpx
import pytest

from lingnexus.common.errors import Conflict
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime


def _profile(family=APIFamily.OPENAI_CHAT_COMPLETIONS):
    return ModelProfile(api_family=family, model="fixture", base_url="https://fixture.test")


def _configure(app, family=APIFamily.OPENAI_CHAT_COMPLETIONS):
    item = app.profiles.save("recovery-fixture", _profile(family), api_key="recovery-secret")
    app.profiles.select(item["id"], None)
    return item["id"]


def _sse(*events):
    return "".join("data: " + json.dumps(item) + "\n\n" for item in events) + "data: [DONE]\n\n"


def _chat_tool_response(path):
    return _sse(
        {"id": "native-1", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "checked source", "content": "reading"}, "finish_reason": None}]},
        {"id": "native-1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "read-call", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]}, "finish_reason": "tool_calls"}]},
    )


def _chat_reply(text="done"):
    return _sse({"id": "native-2", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})


def _native_response(family, path):
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        return _chat_tool_response(path)
    if family == APIFamily.OPENAI_RESPONSES:
        response = {"id": "resp-1", "status": "completed", "output": [
            {"id": "rs_opaque", "type": "reasoning", "summary": [], "encrypted_content": "opaque-reasoning"},
            {"id": "fc_1", "type": "function_call", "status": "completed", "call_id": "read-call",
             "name": "read_file", "arguments": json.dumps({"path": path})},
        ]}
        return _sse({"type": "response.completed", "response": response})
    return _sse(
        {"type": "message_start", "message": {"id": "msg-1", "type": "message", "role": "assistant", "content": [], "model": "fixture", "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "private reasoning", "signature": "native-signature"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "read-call", "name": "read_file", "input": {"path": path}}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    )


def _final_response(family):
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        return _chat_reply()
    if family == APIFamily.OPENAI_RESPONSES:
        return _sse({"type": "response.completed", "response": {"id": "resp-2", "status": "completed", "output": []}})
    return _sse(
        {"type": "message_start", "message": {"id": "msg-2", "type": "message", "role": "assistant", "content": [], "model": "fixture", "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "done"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    )


def _mock_client(responses, requests=None, *, hang=None):
    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        if hang is not None and not hang.is_set():
            hang.set()
            class BlockingStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    await asyncio.Future()
                    yield b""

                async def aclose(self):
                    return None

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BlockingStream())
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=responses.pop(0))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _grant(app, agent_id, folder):
    folder.mkdir(exist_ok=True)
    app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)


def _interrupt_run(app, agent_id, request_id, *, folder=None):
    if folder is not None:
        _grant(app, agent_id, folder)
    reached = asyncio.Event()

    async def scenario():
        async with _mock_client([], hang=reached) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="recover fixture", request_id=request_id))
            await asyncio.wait_for(reached.wait(), 5)
            await asyncio.sleep(0.05)
            await runtime.aclose()
            return run["id"]

    return asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_native_complete_response_and_tool_fact_reach_next_model_request(app, agent_id, tmp_path, family):
    _configure(app, family)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    target = folder / "fact.txt"
    target.write_text("durable tool fact", encoding="utf-8")
    first = _native_response(family, str(target))
    requests = []

    async def scenario():
        async with _mock_client([first, _final_response(family)], requests) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="read fact", request_id="native-follow-up"))
            events = await asyncio.wait_for(asyncio.create_task(_drain(runtime, run["id"])), 5)
            assert not any(event["event"] == "error" for event in events), events
            await runtime.aclose()

    asyncio.run(scenario())
    assert len(requests) == 2
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        assistant = next(m for m in requests[1]["messages"] if m.get("role") == "assistant" and m.get("tool_calls"))
        assert assistant["reasoning_content"] == "checked source"
        assert any(m.get("tool_call_id") == "read-call" and "durable tool fact" in m.get("content", "") for m in requests[1]["messages"])
    elif family == APIFamily.OPENAI_RESPONSES:
        assert any(item.get("encrypted_content") == "opaque-reasoning" for item in requests[1]["input"])
        assert any(item.get("call_id") == "read-call" and "durable tool fact" in item.get("output", "") for item in requests[1]["input"])
    else:
        assistant = next(m for m in requests[1]["messages"] if m.get("role") == "assistant")
        thinking = next(block for block in assistant["content"] if block.get("type") == "thinking")
        assert thinking["signature"] == "native-signature"
        assert any(block.get("type") == "tool_result" and "durable tool fact" in block.get("content", "") for m in requests[1]["messages"] for block in m.get("content", []) if isinstance(m.get("content"), list))


async def _drain(runtime, run_id):
    return [event async for event in runtime.events(run_id)]


def test_recovery_replays_checkpoint_without_resending_user_message(app, agent_id):
    _configure(app)
    reached = asyncio.Event()
    requests_before = []
    requests_after = []

    async def scenario():
        async with _mock_client([], requests_before, hang=reached) as first_client:
            runtime = ChatRuntime(app, client=first_client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="original user input", request_id="recover-once"))
            await asyncio.wait_for(reached.wait(), 5)
            await asyncio.sleep(0.05)
            await runtime.aclose()
        assert runtime.get_run(run["id"])["status"] == "interrupted"
        async with _mock_client([_chat_reply("resumed")], requests_after) as second_client:
            resumed_runtime = ChatRuntime(app, client=second_client)
            recovered = resumed_runtime.recover(run["id"])
            assert recovered["status"] == "running"
            await asyncio.wait_for(asyncio.create_task(_drain(resumed_runtime, run["id"])), 5)
            await resumed_runtime.aclose()
        result = resumed_runtime.get_run(run["id"])
        assert result["status"] == "completed"

    asyncio.run(scenario())
    assert len(requests_before) == 1
    assert len(requests_after) == 1
    users = [m.get("content", "") for m in requests_after[0]["messages"] if m.get("role") == "user"]
    assert len(users) == 1
    assert users[0].startswith("original user input")


def test_backup_switch_checkpoint_can_recover_after_backup_request_interrupts(app, agent_id):
    primary = app.profiles.save("primary", _profile().model_copy(update={"model": "primary"}), api_key="primary-secret")
    backup = app.profiles.save("backup", _profile().model_copy(update={"model": "backup"}), api_key="backup-secret")
    app.profiles.select(primary["id"], backup["id"])
    backup_started = asyncio.Event()
    requests = []

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Future()
            yield b""

        async def aclose(self):
            return None

    def initial_handler(request):
        payload = json.loads(request.content)
        requests.append(payload["model"])
        if payload["model"] == "primary":
            return httpx.Response(503, json={"error": {"type": "server_error"}})
        backup_started.set()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BlockingStream())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(initial_handler)) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="try backup", request_id="backup-recovery"))
            await asyncio.wait_for(backup_started.wait(), 5)
            await asyncio.sleep(0.05)
            await runtime.aclose()
        checkpoint = runtime.checkpoints.load(run["id"])
        assert checkpoint["profile"]["model"] == "backup"
        assert checkpoint["phase"] == "model_inflight"
        assert app.profiles.get(runtime.get_run(run["id"])["profile_id"])["profile"]["model"] == "backup"
        async with _mock_client([_chat_reply("backup resumed")]) as resumed_client:
            resumed_runtime = ChatRuntime(app, client=resumed_client)
            assert resumed_runtime.recover(run["id"])["status"] == "running"
            await asyncio.wait_for(asyncio.create_task(_drain(resumed_runtime, run["id"])), 5)
            assert resumed_runtime.get_run(run["id"])["status"] == "completed"
            await resumed_runtime.aclose()

    asyncio.run(scenario())
    assert requests == ["primary", "backup"]


def test_first_request_respects_window_including_output_and_tool_declarations(app, agent_id):
    profile = ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
                           base_url="https://fixture.test", context_window_tokens=1000)
    item = app.profiles.save("too-small", profile, api_key="recovery-secret")
    app.profiles.select(item["id"], None)
    requests = []

    async def scenario():
        async with _mock_client([], requests) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="hello", request_id="first-overflow"))
            events = await asyncio.wait_for(asyncio.create_task(_drain(runtime, run["id"])), 5)
            assert next(e["data"]["code"] for e in events if e["event"] == "error") == "context_limit"
            assert requests == []
            await runtime.aclose()

    asyncio.run(scenario())


def test_context_limit_run_is_not_recoverable_and_explains_next_action(app, agent_id, tmp_path):
    small = ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture", base_url="https://fixture.test", context_window_tokens=32768)
    item = app.profiles.save("small-context", small, api_key="recovery-secret")
    app.profiles.select(item["id"], None)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    target = folder / "large.txt"
    target.write_text("context expansion " * 20_000, encoding="utf-8")
    first = _chat_tool_response(str(target))

    async def scenario():
        async with _mock_client([first]) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="read large file", request_id="context-limit"))
            events = await asyncio.wait_for(asyncio.create_task(_drain(runtime, run["id"])), 5)
            error = next(event["data"] for event in events if event["event"] == "error")
            assert error["code"] == "context_limit"
            status = runtime.get_run(run["id"])
            assert status["recoverable"] is False
            assert "拆分任务或新建请求" in status["recovery_reason"]
            with pytest.raises(Conflict, match="拆分任务或新建请求"):
                runtime.recover(run["id"])
            await runtime.aclose()

    asyncio.run(scenario())


def test_follow_up_reuses_native_tool_fact_but_fork_drops_old_branch(app, agent_id, tmp_path):
    _configure(app)
    folder = tmp_path / "authorized"
    _grant(app, agent_id, folder)
    target = folder / "fact.txt"
    target.write_text("branch-only tool fact", encoding="utf-8")
    requests = []

    async def scenario():
        async with _mock_client([
            _chat_tool_response(str(target)), _chat_reply("first done"),
            _chat_reply("follow-up done"), _chat_reply("fork done"),
        ], requests) as client:
            runtime = ChatRuntime(app, client=client)
            first = runtime.submit(ChatRequest(agent_id=agent_id, message="read the file", request_id="history-first"))
            await _drain(runtime, first["id"])
            conversation_id = first["conversation_id"]
            user_id = first["user_message_id"]
            second = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=conversation_id, message="use the result", request_id="history-next"))
            await _drain(runtime, second["id"])
            app.conversations.edit_user(conversation_id, user_id, "edited branch prompt", request_id="fork-edit")
            fork = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=conversation_id, message="new branch only", request_id="history-fork"))
            await _drain(runtime, fork["id"])
            await runtime.aclose()

    asyncio.run(scenario())
    assert len(requests) == 4
    assert "branch-only tool fact" in json.dumps(requests[2], ensure_ascii=False)
    assert "branch-only tool fact" not in json.dumps(requests[3], ensure_ascii=False)
    assert "read-call" not in json.dumps(requests[3], ensure_ascii=False)


def test_recovery_refuses_missing_checkpoint_and_executing_effect(app, agent_id):
    _configure(app)

    async def scenario():
        async with _mock_client([_chat_reply()]) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="legacy", request_id="old-run"))
            await asyncio.wait_for(asyncio.create_task(_drain(runtime, run["id"])), 5)
            with app.database.transaction() as db:
                db.execute("DELETE FROM chat_run_checkpoints WHERE run_id=?", (run["id"],))
            with pytest.raises(Conflict):
                runtime.recover(run["id"])
            await runtime.aclose()

    asyncio.run(scenario())


def test_recovery_refuses_effect_left_executing(app, agent_id):
    _configure(app)
    run_id = _interrupt_run(app, agent_id, "effect-inflight")
    with app.database.transaction() as db:
        db.execute("INSERT INTO chat_tool_effects(run_id,step,call_id,status) VALUES(?,1,'uncertain','executing')", (run_id,))
    runtime = ChatRuntime(app)
    assert runtime.get_run(run_id)["recoverable"] is False
    async def attempt():
        with pytest.raises(Conflict):
            runtime.recover(run_id)
        await runtime.aclose()
    asyncio.run(attempt())


def test_recovery_keeps_unknown_failure_as_nonrecoverable(app, agent_id):
    _configure(app)
    run_id = _interrupt_run(app, agent_id, "unknown-effect")
    runtime = ChatRuntime(app)
    checkpoint = runtime.checkpoints.load(run_id)
    checkpoint.setdefault("evidence", {})["unknown_failures"] = ["unresolved result"]
    runtime.checkpoints.save(run_id, checkpoint)
    async def attempt():
        assert runtime.get_run(run_id)["recoverable"] is False
        with pytest.raises(Conflict):
            runtime.recover(run_id)
        await runtime.aclose()
    asyncio.run(attempt())


@pytest.mark.parametrize("changed", ["profile", "workspace"])
def test_recovery_refuses_changed_model_or_workspace(app, agent_id, tmp_path, changed):
    profile_id = _configure(app)
    folder = tmp_path / "work" if changed == "workspace" else None
    run_id = _interrupt_run(app, agent_id, "changed-" + changed, folder=folder)
    if changed == "profile":
        # A change to persisted profile fields must invalidate the native checkpoint.
        app.profiles.save("recovery-fixture", _profile().model_copy(update={"model": "changed"}), api_key="recovery-secret", profile_id=profile_id)
    else:
        (folder / "external.txt").write_text("changed outside the run", encoding="utf-8")
    runtime = ChatRuntime(app)
    async def attempt():
        with pytest.raises(Conflict):
            runtime.recover(run_id)
        await runtime.aclose()
    asyncio.run(attempt())


def test_recovery_endpoint_requires_owner_auth(client):
    response = client.post("/api/chat/runs/not-a-run/recover", json={})
    assert response.status_code == 401


def test_full_backup_preserves_private_checkpoint_without_publicly_exposing_it(app, agent_id, tmp_path):
    _configure(app)

    async def finish_run():
        async with _mock_client([_chat_reply()]) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="backup checkpoint", request_id="backup-checkpoint"))
            await asyncio.wait_for(asyncio.create_task(_drain(runtime, run["id"])), 5)
            await runtime.aclose()
            return run["id"]

    run_id = asyncio.run(finish_run())
    from lingnexus.app import Application
    from lingnexus.maintenance.service import MaintenanceService

    backup = tmp_path / "complete.db"
    restored_path = tmp_path / "restored.db"
    MaintenanceService(app.database).backup_to(backup)
    result = MaintenanceService(app.database).restore_backup(backup, restored_path)
    assert result["restored"]
    restored_app = Application(restored_path)
    try:
        with restored_app.database.transaction() as db:
            raw = db.execute("SELECT state FROM chat_run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
            assert raw is not None
            state = json.loads(raw[0])
            assert "backup checkpoint" in json.dumps(state, ensure_ascii=False)
        runtime = ChatRuntime(restored_app)
        public_run = runtime.get_run(run_id)
        assert public_run["status"] == "completed"
        assert "checkpoint" not in public_run
        assert "payload" not in public_run
        asyncio.run(runtime.aclose())
    finally:
        restored_app.close()
