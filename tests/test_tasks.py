"""Durable task integration tests; only the external HTTP boundary is synthetic."""
import asyncio
import json

import httpx
import pytest

from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.models import ModelProfile


def chat(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {"index": i, "id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (cid, name, args) in enumerate(calls)
        ]
    frame = {"id": "chat-native", "choices": [{"index": 0, "delta": delta,
              "finish_reason": "tool_calls" if calls else "stop"}]}
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def configure(app, family="openai_chat_completions"):
    profile = ModelProfile(api_family=family, model="fixture", base_url="https://fixture.test")
    pid = app.profiles.save("fixture", profile, api_key="runtime-secret-only")["id"]
    app.profiles.select(pid, None)


def client_for(responses, requests):
    def handler(request):
        requests.append(json.loads(request.content))
        value = responses.pop(0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=value)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def settle(runtime, tid):
    for _ in range(1500):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            return runtime.get(tid)
        await asyncio.sleep(.01)
    pytest.fail("task worker did not reach a checkpoint")


def test_real_file_multistep_idempotency_events_and_private_state(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TASK_TABLES, TaskRuntime
        configure(app)
        folder = tmp_path / "authorized"
        folder.mkdir()
        target = folder / "answer.txt"
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        requests = []
        replies = [chat("Plan: write then check.", [("write-id", "create_file",
                    {"path": str(target), "text": "actual artifact"})]),
                   chat(calls=[("check-id", "read_file", {"path": str(target)})]),
                   chat("Delivered and checked answer.txt.")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            spec = {"agent_id": agent_id, "objective": "Create and verify answer.txt",
                    "request_id": "durable-one"}
            tid = await runtime.submit(spec)
            assert await runtime.submit(spec) == tid
            with pytest.raises(Conflict):
                await runtime.submit({**spec, "objective": "changed"})
            result = await settle(runtime, tid)
            assert result["status"] == "completed", result
            assert result["verified"] is True
            assert target.read_text() == "actual artifact"
            assert len(requests) == 3
            assert requests[1]["messages"][-1]["tool_call_id"] == "write-id"
            assert requests[2]["messages"][-1]["tool_call_id"] == "check-id"
            events = runtime.events(tid)
            assert {"plan", "tool_started", "tool_result", "final", "status"} <= {e["type"] for e in events}
            assert runtime.events(tid, after=events[-1]["id"]) == []
            with app.database.transaction() as db:
                activity = db.execute("SELECT content FROM activity_events WHERE dedupe_key=?", ("task:" + tid,)).fetchall()
                assert len(activity) == 1 and json.loads(activity[0][0])["verified"] is True
                assert db.execute("SELECT count(*) FROM task_steps WHERE task_id=? AND effect_id IS NOT NULL", (tid,)).fetchone()[0] == 2
                for table in TASK_TABLES:
                    rows = db.execute("SELECT * FROM " + table).fetchall()
                    assert "runtime-secret-only" not in repr([tuple(r) for r in rows])
            await runtime.close()
    asyncio.run(run())


def test_foreground_priority_and_command_without_snapshot_never_runs_host(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        busy, requests = [True], []
        target = tmp_path / "host-effect.txt"
        replies = [chat(calls=[("cmd", "run_command", {"argv": ["python", "-c", f"open({str(target)!r}, 'w').write('unsafe')"], "purpose": "verify"})]), chat("Passed")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client, foreground_busy=lambda: busy[0])
            tid = await runtime.submit({"agent_id": agent_id, "objective": "test isolation", "request_id": "priority"})
            await asyncio.sleep(.15)
            assert not requests
            await runtime.pause(tid)
            assert runtime.get(tid)["status"] == "paused"
            busy[0] = False
            await runtime.resume(tid)
            state = await settle(runtime, tid)
            assert state["status"] == "needs_review" and not target.exists()
            results = [e["data"]["result"] for e in runtime.events(tid) if e["type"] == "tool_result"]
            assert results[0]["status"] == "failed" and results[0]["error_code"] == "permission_denied"
            await runtime.close()
    asyncio.run(run())


def test_reading_old_file_cannot_clear_a_denied_write(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        folder = tmp_path / "read-only"
        folder.mkdir()
        file = folder / "old.txt"
        file.write_text("original")
        app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
        requests = []
        replies = [chat(calls=[("denied", "write_file", {"path": str(file), "text": "changed"})]), chat(calls=[("read", "read_file", {"path": str(file)})]), chat("Updated and verified")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "modify file", "request_id": "denied-write"})
            state = await settle(runtime, tid)
            assert state["status"] == "needs_review" and not state["verified"]
            assert file.read_text() == "original"
            await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["invalid_args", "credential_echo", "truncated_final", "eof"])
def test_no_effect_from_invalid_or_incomplete_model_response(app, agent_id, tmp_path, mode):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        folder = tmp_path / "invalid"
        folder.mkdir()
        target = folder / "forbidden.txt"
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        if mode == "invalid_args":
            frame = {"id": "bad", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "bad", "type": "function", "function": {"name": "create_file", "arguments": "not-json"}}]}, "finish_reason": "tool_calls"}]}
            response = sse([frame]) + "data: [DONE]\n\n"
        elif mode == "credential_echo":
            response = chat("runtime-secret-only", [("bad", "create_file", {"path": str(target), "text": "x"})])
        elif mode == "eof":
            response = chat(calls=[("eof", "create_file", {"path": str(target), "text": "never execute"})]).split("data: [DONE]")[0]
        else:
            target.write_text("already there")
            response = chat("All verified").replace('"finish_reason": "stop"', '"finish_reason": "length"')
        requests = []
        async with client_for([response], requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "create file", "request_id": "invalid"})
            state = await settle(runtime, tid)
            assert state["status"] == "blocked", state
            assert state["error_code"] == {"invalid_args": "model_invalid_tool_arguments", "credential_echo": "credential_echo", "truncated_final": "model_incomplete", "eof": "model_unexpected_eof"}[mode]
            assert target.read_text() == "already there" if mode == "truncated_final" else not target.exists()
            assert "runtime-secret-only" not in repr(runtime.events(tid))
            await runtime.close()
    asyncio.run(run())


def test_crash_reconciles_known_action_but_not_unknown_command(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        from lingnexus.tasks.protocol import append_response
        configure(app)
        folder = tmp_path / "crash-state"
        folder.mkdir()
        target = folder / "once.txt"
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        requests = []
        async with client_for([chat("Delivered")], requests) as client:
            first = TaskRuntime(app, client)
            tid = await first.submit({"agent_id": agent_id, "objective": "create once", "request_id": "known"})
            await first.pause(tid)
            args = {"path": str(target), "text": "once"}
            call = {"id": "known-call", "name": "create_file", "arguments": args}
            state = first.store.load(tid)
            native = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "known-call", "type": "function", "function": {"name": "create_file", "arguments": json.dumps(args)}}]}}]}
            append_response(state["payload"], state["family"], native)
            state["pending"] = [call]
            first.store.save(tid, state, model_step=True)
            action = app.actions.execute(agent_id, "task:" + tid + ":1:known-call", "create_file", args)
            step = first.store.step(tid, 1, call)
            first.store.executing(step["id"], action["id"])
            target.write_text("owner changed after known effect")
            with app.database.transaction() as db:
                db.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
            await first.close()
            recovered = TaskRuntime(app, client)
            await recovered.start()
            assert recovered.get(tid)["status"] == "interrupted"
            await recovered.resume(tid)
            assert (await settle(recovered, tid))["status"] == "needs_review"
            assert target.read_text() == "owner changed after known effect"
            assert requests[0]["messages"][-1]["tool_call_id"] == "known-call"
            assert "completed" in requests[0]["messages"][-1]["content"]
            other = await recovered.submit({"agent_id": agent_id, "objective": "command", "request_id": "unknown-command"})
            await recovered.pause(other)
            call = {"id": "uncertain", "name": "run_command", "arguments": {"argv": ["python", "-V"], "purpose": "verify"}}
            state = recovered.store.load(other)
            state["pending"] = [call]
            recovered.store.save(other, state, model_step=True)
            step = recovered.store.step(other, 1, call)
            recovered.store.executing(step["id"])
            await recovered.resume(other)
            result = await settle(recovered, other)
            assert result["status"] == "blocked" and result["error_code"] == "effect_needs_review"
            assert len(requests) == 1
            await recovered.close()
    asyncio.run(run())


def test_owner_api_review_artifacts_and_durable_events(app, agent_id, tmp_path):
    async def run():
        from fastapi import FastAPI

        from lingnexus.api.tasks import router
        from lingnexus.tasks import TaskRuntime
        configure(app)
        folder = tmp_path / "api-project"
        folder.mkdir()
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        requests = []
        replies = [chat(calls=[("create", "create_file", {"path": "answer.txt", "text": "artifact"})]), chat(calls=[("read", "read_file", {"path": "answer.txt"})]), chat("Delivered")]
        async with client_for(replies, requests) as model_client:
            runtime = TaskRuntime(app, model_client)
            server = FastAPI()
            server.state.tasks, server.state.owner_token, server.state.allowed_origins = runtime, "owner-token", []
            server.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://local") as browser:
                spec = {"agent_id": agent_id, "objective": "create artifact", "request_id": "api", "workspace": str(folder)}
                assert (await browser.post("/tasks", json=spec)).status_code == 401
                headers = {"Authorization": "Bearer owner-token"}
                response = await browser.post("/tasks", json=spec, headers=headers)
                assert response.status_code == 202
                tid = response.json()["id"]
                assert (await settle(runtime, tid))["status"] == "needs_review"
                exposed = (await browser.get("/tasks/" + tid, headers=headers)).json()
                assert "payload" not in exposed and "continuation" not in exposed
                events = (await browser.get("/tasks/" + tid + "/events?after=0", headers=headers)).json()
                assert events[-1]["data"]["status"] == "needs_review"
                assert (await browser.get(f"/tasks/{tid}/events?after={events[-1]['id']}", headers=headers)).json() == []
                streamed = await browser.get(f"/tasks/{tid}/stream", headers=headers)
                assert "event: final" in streamed.text
                changes = (await browser.get(f"/tasks/{tid}/changes", headers=headers)).json()
                artifact = await browser.get(f"/tasks/{tid}/artifacts/answer.txt", headers=headers)
                assert artifact.content == b"artifact" and not (folder / "answer.txt").exists()
                applied = await browser.post(f"/tasks/{tid}/apply", json={"review_id": changes["review_id"]}, headers=headers)
                assert applied.status_code == 200 and (folder / "answer.txt").read_text() == "artifact"
            await runtime.close()
    asyncio.run(run())


def test_snapshot_size_and_links_rejected_and_grants_rechecked(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        from lingnexus.tools import FileAccessDenied, FileLimitExceeded
        configure(app)
        folder = tmp_path / "boundary"
        folder.mkdir()
        file = folder / "source.txt"
        file.write_bytes(b"x" * 1_048_577)
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        requests = []
        async with client_for([], requests) as client:
            runtime = TaskRuntime(app, client, foreground_busy=lambda: True)
            spec = {"agent_id": agent_id, "objective": "snapshot", "request_id": "caps", "workspace": str(folder)}
            with pytest.raises(FileLimitExceeded):
                await runtime.submit(spec)
            file.write_text("original")
            import os
            os.link(file, folder / "alias.txt")
            with pytest.raises(FileAccessDenied):
                await runtime.submit(spec)
            (folder / "alias.txt").unlink()
            tid = await runtime.submit(spec)
            await runtime.pause(tid)
            runtime.workspace(tid).joinpath("source.txt").write_text("generated")
            review = runtime.changes(tid)
            app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=1)
            with pytest.raises(PermissionDenied):
                await runtime.apply(tid, review_id=review["review_id"])
            assert file.read_text() == "original" and not requests
            runtime.foreground_busy = lambda: False
            await runtime.resume(tid)
            state = await settle(runtime, tid)
            assert state["status"] == "blocked" and state["error_code"] == "permissions_revoked"
            assert not requests
            await runtime.close()
    asyncio.run(run())





def test_limits_pause_resume_cancel_and_no_false_success(app, agent_id):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        requests = []
        replies = [chat(calls=[("denied", "read_file", {"path": "/denied.txt"})]),
                   chat("Everything passed!")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client, max_steps=1)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "check", "request_id": "limit"})
            state = await settle(runtime, tid)
            assert state["status"] == "paused" and state["error_code"] == "step_limit"
            await runtime.resume(tid, additional_steps=2)
            state = await settle(runtime, tid)
            assert state["status"] == "needs_review" and not state["verified"]
            other = await runtime.submit({"agent_id": agent_id, "objective": "later", "request_id": "cancel"})
            await runtime.pause(other)
            assert runtime.get(other)["status"] == "paused"
            await runtime.cancel(other)
            assert runtime.get(other)["status"] == "cancelled"
            await runtime.close()
    asyncio.run(run())


def test_snapshot_requires_both_grants_and_review_conflicts(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        work = tmp_path / "project"
        work.mkdir()
        (work / "main.py").write_text("old")
        (work / ".env.local").write_text("secret")
        (work / "node_modules").mkdir()
        (work / "node_modules" / "ignore").write_text("dependency")
        app.permissions.configure(agent_id, readable=[work], writable=[], expected_revision=0)
        requests = []
        replies = [chat(calls=[("edit", "write_file", {"path": "main.py", "text": "new"})]),
                   chat(calls=[("read", "read_file", {"path": "/workspace/main.py"})]), chat("Checked staged main.py")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            spec = {"agent_id": agent_id, "objective": "update project", "request_id": "snapshot", "workspace": str(work)}
            with pytest.raises(PermissionDenied):
                await runtime.submit(spec)
            app.permissions.configure(agent_id, readable=[work], writable=[work], expected_revision=1)
            tid = await runtime.submit(spec)
            state = await settle(runtime, tid)
            assert state["status"] == "needs_review" and not state["verified"], state
            assert (work / "main.py").read_text() == "old"
            changes = runtime.changes(tid)
            assert [c["path"] for c in changes["files"]] == ["main.py"]
            assert "-old" in changes["files"][0]["diff"]
            assert not (runtime.workspace(tid) / ".env.local").exists()
            assert not (runtime.workspace(tid) / "node_modules").exists()
            (work / "main.py").write_text("owner edit")
            with pytest.raises(Conflict):
                await runtime.apply(tid, review_id=changes["review_id"])
            assert (work / "main.py").read_text() == "owner edit"
            (work / "main.py").write_text("old")
            applied = await runtime.apply(tid, review_id=changes["review_id"])
            assert applied["status"] == "applied"
            assert (work / "main.py").read_text() == "new"
            assert await runtime.apply(tid, review_id=changes["review_id"]) == applied
            await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["before_model", "before_tool", "tool_return"])
def test_changed_or_withdrawn_self_blocks_stale_plans(app, agent_id, tmp_path, stage):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        folder = tmp_path / "self-fence"
        folder.mkdir()
        target = folder / "stale.txt"
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        ref = app.history.add(agent_id, "identity evidence", kind="user", scope="real", request_id="self-evidence")
        proposal = app.identity.propose(agent_id, kind="identity", key="identity.name", text="Pinned name",
                                        sources=[ref], base_revision=0, request_id="self-proposal")
        if stage == "model_return":
            app.identity.decide(proposal["id"], "approve", expected_revision=0)
        requests = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            requests.append(json.loads(request.content))
            if stage == "model_return":
                entered.set()
                await release.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=chat(calls=[
                ("stale-write", "create_file", {"path": str(target), "text": "stale plan"})]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            spec = {"agent_id": agent_id, "objective": "write using current self", "request_id": stage}
            tid = await runtime.submit(spec)
            if stage in {"before_model", "before_tool"}:
                await runtime.pause(tid)
                if stage == "before_tool":
                    state = runtime.store.load(tid)
                    state["pending"] = [{"id": "stale-write", "name": "create_file", "arguments": {"path": str(target), "text": "stale plan"}}]
                    runtime.store.save(tid, state, model_step=True)
                app.identity.decide(proposal["id"], "approve", expected_revision=0)
                await runtime.resume(tid)
            elif stage == "model_return":
                await asyncio.wait_for(entered.wait(), 2)
                app.history.correct(ref, "withdraw identity evidence")
                release.set()
            else:
                original = runtime.executor.execute
                async def execute_then_change(task, call):
                    result = await original(task, call)
                    await asyncio.sleep(0)
                    app.identity.decide(proposal["id"], "approve", expected_revision=0)
                    return result
                runtime.executor.execute = execute_then_change
            state = await settle(runtime, tid)
            assert state["status"] == "blocked" and state["error_code"] == "self_changed", state
            checkpoint = runtime.store.load(tid)
            assert checkpoint["self_revision"] == (1 if stage == "model_return" else 0)
            if stage == "tool_return":
                assert target.read_text() == "stale plan"
                assert not checkpoint["pending"]
                assert checkpoint["payload"]["messages"][-1]["tool_call_id"] == "stale-write"
                assert any(e["type"] == "tool_result" for e in runtime.events(tid))
            else:
                assert not target.exists()
                assert not any(e["type"] == "tool_started" for e in runtime.events(tid))
            count = len(requests)
            await runtime.resume(tid)
            assert (await settle(runtime, tid))["error_code"] == "self_changed"
            assert len(requests) == count
            await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize(("family", "finish", "expected"), [
    ("openai_chat_completions", "end_turn", "blocked"),
    ("anthropic_messages", "stop", "blocked"),
    ("anthropic_messages", "stop_sequence", "completed"),
])
def test_finish_reason_must_belong_to_selected_api_family(app, agent_id, tmp_path, family, finish, expected):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app, family)
        folder = tmp_path / "family"
        folder.mkdir()
        file = folder / "checked.txt"
        file.write_text("proof")
        app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
        if family == "openai_chat_completions":
            replies = [chat(calls=[("read", "read_file", {"path": str(file)})]), chat("Checked").replace('"finish_reason": "stop"', '"finish_reason": "' + finish + '"')]
        else:
            replies = [anthropic([{"type": "tool_use", "id": "read", "name": "read_file", "input": {"path": str(file)}}]), anthropic([{"type": "text", "text": "Checked"}]).replace('"stop_reason": "end_turn"', '"stop_reason": "' + finish + '"')]
        async with client_for(replies, []) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "check", "request_id": "finish"})
            result = await settle(runtime, tid)
            assert result["status"] == expected, result
            assert result["verified"] == (expected == "completed")
            await runtime.close()
    asyncio.run(run())


def test_recovery_preserves_checkpoint_and_never_replays_unknown_effect(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        work = tmp_path / "recover"
        work.mkdir()
        target = work / "new.txt"
        app.permissions.configure(agent_id, readable=[work], writable=[work], expected_revision=0)
        requests = []
        replies = [chat(calls=[("effect", "create_file", {"path": str(target), "text": "once"})]), chat("done")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "create", "request_id": "crash"})
            await runtime.pause(tid)
            intent = app.actions.prepare(agent_id, "task:" + tid + ":1:effect", "create_file",
                                         {"path": str(target), "text": "once"})
            app.actions.mark_executing(intent["id"])
            await runtime.resume(tid)
            state = await settle(runtime, tid)
            assert state["status"] == "blocked" and state["error_code"] == "effect_needs_review", state
            assert not target.exists()
            await runtime.close()
    asyncio.run(run())


def sse(frames):
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)


def anthropic(blocks):
    events = [{"type": "message_start", "message": {"id": "msg-fixture", "role": "assistant",
               "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}]
    for i, block in enumerate(blocks):
        events.append({"type": "content_block_start", "index": i, "content_block": block})
        events.append({"type": "content_block_stop", "index": i})
    events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use" if any(
        b["type"] == "tool_use" for b in blocks) else "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"}])
    return sse(events)


@pytest.mark.parametrize("family", ["anthropic_messages", "openai_responses"])
def test_native_opaque_continuation_survives_worker_restart(app, agent_id, tmp_path, family):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app, family)
        folder = tmp_path / "native"
        folder.mkdir()
        target = folder / "evidence.txt"
        target.write_text("native evidence")
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        blocks = [{"type": "thinking", "thinking": "private", "signature": "opaque-signature"},
                  {"type": "tool_use", "id": "native-call", "name": "read_file", "input": {"path": str(target)}}]
        output = [{"type": "reasoning", "id": "reasoning-item", "encrypted_content": "opaque-encrypted", "summary": []},
                  {"type": "function_call", "id": "function-item", "call_id": "native-call", "name": "read_file", "arguments": json.dumps({"path": str(target)})}]
        first = anthropic(blocks) if family == "anthropic_messages" else sse([
            {"type": "response.completed", "response": {"id": "response-fixture", "status": "completed", "output": output}}])
        final = anthropic([{"type": "text", "text": "Verified evidence."}]) if family == "anthropic_messages" else sse([
            {"type": "response.completed", "response": {"id": "response-final", "status": "completed", "output": [
                {"type": "message", "id": "final-item", "role": "assistant", "content": [{"type": "output_text", "text": "Verified evidence."}]}]}}])
        requests = []
        async with client_for([first, final], requests) as client:
            runtime = TaskRuntime(app, client, max_steps=1)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "read and verify", "request_id": "native"})
            assert (await settle(runtime, tid))["status"] == "paused"
            await runtime.close()
            recovered = TaskRuntime(app, client)
            await recovered.start()
            assert recovered.get(tid)["status"] == "paused"
            await recovered.resume(tid, additional_steps=1)
            state = await settle(recovered, tid)
            assert state["status"] == "completed", state
            body = requests[1]
            if family == "anthropic_messages":
                assert body["messages"][1]["content"] == blocks
                assert body["messages"][2]["content"][0]["tool_use_id"] == "native-call"
            else:
                assert body["input"][1:3] == output
                assert body["input"][3]["call_id"] == "native-call"
                assert body["store"] is False
            await recovered.close()
    asyncio.run(run())


def test_skills_read_checks_active_ownership_and_catalog_has_no_approval(app, agent_id):
    async def run():
        from lingnexus.skills.service import Skills
        from lingnexus.tasks import TaskRuntime
        app.skills = Skills(app.database, app.history)
        configure(app)
        source = app.history.add(agent_id, "Verified a procedure", kind="tool", scope="real", request_id="skill-evidence")
        skill = app.skills.save(agent_id, name="Check procedure", description="Always inspect results",
                                instructions="Read source evidence first.", sources=[source], request_id="skill")
        app.skills.decide(skill["id"], "activate", expected_revision=1)
        other = app.create_agent("other")
        other_source = app.history.add(other, "other evidence", kind="tool", scope="real", request_id="other-source")
        foreign = app.skills.save(other, name="Foreign", description="Not available", instructions="Forbidden",
                                  sources=[other_source], request_id="foreign")
        requests = []
        replies = [chat(calls=[("read-skill", "read_skill", {"skill_id": skill["id"]})]),
                   chat(calls=[("foreign-skill", "read_skill", {"skill_id": foreign["id"]})]), chat("checked")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "Use my procedure", "request_id": "skill-task"})
            state = await settle(runtime, tid)
            assert state["status"] == "needs_review"
            assert "Always inspect results" in requests[0]["messages"][0]["content"]
            assert "read_skill" in repr(requests[0]["tools"])
            assert "approve" not in {tool["function"]["name"] for tool in requests[0]["tools"]}
            assert all("approve" not in tool["function"]["parameters"].get("properties", {}) for tool in requests[0]["tools"])
            assert "Read source evidence first." in requests[1]["messages"][-1]["content"]
            assert "Forbidden" not in repr(requests)
            await runtime.close()
    asyncio.run(run())


def test_environment_selection_keeps_legacy_python_fingerprint_and_pins_images(app, agent_id):
    async def run():
        import hashlib

        from lingnexus.tasks import TaskRuntime
        from lingnexus.tasks.service import Submission
        from lingnexus.tasks.store import encode
        configure(app)
        async with client_for([], []) as client:
            runtime = TaskRuntime(app, client, docker_image="python:owner", node_image="node:owner", foreground_busy=lambda: True)
            spec = {"agent_id": agent_id, "objective": "check project", "request_id": "old-python"}
            tid = await runtime.submit(spec)
            await runtime.pause(tid)
            legacy = {**spec, "workspace": None}
            fingerprint = hashlib.sha256(encode(legacy).encode()).hexdigest()
            with app.database.transaction() as db:
                row = db.execute("SELECT fingerprint,docker_image FROM tasks WHERE id=?", (tid,)).fetchone()
                assert row["fingerprint"] == fingerprint and row["docker_image"] == "python:owner"
                checkpoint = runtime.store.load(tid)
                checkpoint.pop("environment", None)
                runtime.store.save(tid, checkpoint)
            assert await runtime.submit({**spec, "environment": "python"}) == tid
            assert runtime.get(tid)["environment"] == "python"
            with pytest.raises(Conflict):
                await runtime.submit({**spec, "environment": "node"})
            node_id = await runtime.submit({**spec, "request_id": "new-node", "environment": "node"})
            await runtime.pause(node_id)
            assert runtime.get(node_id)["docker_image"] == "node:owner"
            assert runtime.get(node_id)["environment"] == "node"
            with pytest.raises(ValueError):
                Submission.model_validate({**spec, "environment": "arbitrary-image:tag"})
            await runtime.aclose()
            reopened = TaskRuntime(app, client, node_image="node:different")
            assert reopened.get(node_id)["docker_image"] == "node:owner"
            await reopened.aclose()
    asyncio.run(run())


def test_owner_api_accepts_only_python_or_node_environment(app, agent_id):
    async def run():
        from fastapi import FastAPI

        from lingnexus.api.tasks import router
        from lingnexus.tasks import TaskRuntime
        configure(app)
        async with client_for([], []) as model:
            runtime = TaskRuntime(app, model, foreground_busy=lambda: True)
            server = FastAPI()
            server.state.tasks, server.state.owner_token, server.state.allowed_origins = runtime, "owner", []
            server.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://local", headers={"Authorization": "Bearer owner"}) as owner:
                spec = {"agent_id": agent_id, "objective": "check", "request_id": "api-node", "environment": "node"}
                response = await owner.post("/tasks", json=spec)
                assert response.status_code == 202, response.text
                assert response.json()["environment"] == "node"
                assert response.json()["docker_image"] == "node:24-bookworm-slim"
                await runtime.pause(response.json()["id"])
                invalid = await owner.post("/tasks", json={**spec, "environment": "image:chosen-by-model"})
                assert invalid.status_code == 422
            await runtime.aclose()
    asyncio.run(run())
