import asyncio
import json
import sys

import httpx


def _frame(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {"index": i, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (call_id, name, args) in enumerate(calls)
        ]
    event = {"id": "lifecycle", "choices": [{
        "index": 0, "delta": delta,
        "finish_reason": "tool_calls" if calls else "stop",
    }]}
    return "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"


def test_final_after_running_command_cancels_before_snapshot_can_change(
    app, agent_id, tmp_path
):
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    async def scenario():
        project = tmp_path / "project"
        project.mkdir()
        app.permissions.configure(agent_id, readable=[project], writable=[project], expected_revision=0)
        profile = app.profiles.save(
            "lifecycle", ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model="offline-lifecycle", base_url="https://fixture.invalid/v1",
            ), api_key="fixture-only",
        )
        app.profiles.select(profile["id"], None)
        script = (
            "import pathlib,time; time.sleep(5); "
            "pathlib.Path('late.txt').write_text('must not appear')"
        )
        responses = [
            _frame("Starting the long command.", [("cmd", "run_command", {
                "argv": [sys.executable, "-c", script], "purpose": "work", "long_running": True,
            })]),
            _frame("Finished."),
        ]

        def handler(_request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=responses.pop(0)
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = TaskRuntime(app, client)
        try:
            tid = await runtime.submit({
                "agent_id": agent_id, "objective": "Run the slow command",
                "request_id": "final-during-command", "workspace": str(project),
            })
            for _ in range(500):
                task = runtime.get(tid)
                if task["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert task["status"] == "blocked", task
            assert task["error_code"] in {"command_session_active", "command_session_unknown"}
            records = runtime.sandbox.task_sessions(agent_id, tid)
            assert records and records[0]["status"] in {"cancelled", "unknown"}
            await asyncio.sleep(5.2)
            assert not (runtime.workspace(tid) / "late.txt").exists()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_task_cancel_waits_for_command_cleanup_before_returning(app, agent_id, tmp_path):
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    async def scenario():
        project = tmp_path / "cancel-project"
        project.mkdir()
        app.permissions.configure(agent_id, readable=[project], writable=[project], expected_revision=0)
        profile = app.profiles.save(
            "cancel-lifecycle", ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model="offline-lifecycle", base_url="https://fixture.invalid/v1",
            ), api_key="fixture-only",
        )
        app.profiles.select(profile["id"], None)
        script = (
            "import pathlib,time; time.sleep(5); "
            "pathlib.Path('late.txt').write_text('must not appear')"
        )
        model_requested_again = asyncio.Event()

        async def handler(_request):
            if not model_requested_again.is_set():
                model_requested_again.set()
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    text=_frame("Starting the long command.", [("cmd", "run_command", {
                        "argv": [sys.executable, "-c", script],
                        "purpose": "work", "long_running": True,
                    })]),
                )
            await asyncio.Event().wait()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = TaskRuntime(app, client)
        try:
            tid = await runtime.submit({
                "agent_id": agent_id, "objective": "Cancel the slow command",
                "request_id": "cancel-during-command", "workspace": str(project),
            })
            sessions = []
            for _ in range(200):
                sessions = runtime.sandbox.task_sessions(agent_id, tid)
                if sessions and sessions[0]["status"] in {"running", "cancelling"}:
                    break
                await asyncio.sleep(0.02)
            assert sessions and sessions[0]["status"] in {"running", "cancelling"}
            stopped = await runtime.cancel(tid)
            assert stopped["status"] == "cancelled", stopped
            sessions = runtime.sandbox.task_sessions(agent_id, tid)
            assert sessions[0]["status"] == "cancelled", sessions
            owner = runtime.sandbox._owner(agent_id, tid, "task")
            result = runtime.sandbox.sessions.poll(sessions[0]["session_id"], owner)["result"]
            assert result["cleanup_confirmed"] is True
            await asyncio.sleep(5.2)
            assert not (runtime.workspace(tid) / "late.txt").exists()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_tool_call_without_path_argument_does_not_crash_the_task_runtime(app, agent_id, tmp_path):
    """A file call with no "path" must surface as a failed tool result.

    The runtime used to read call["arguments"]["path"] while canonicalising
    evidence, so a missing key escaped as a KeyError and the worker turned the
    whole task into a fatal task_runtime_error instead of letting the model see
    the rejected call and continue.
    """
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    async def scenario():
        project = tmp_path / "missing-arg-project"
        project.mkdir()
        app.permissions.configure(agent_id, readable=[project], writable=[project], expected_revision=0)
        profile = app.profiles.save(
            "missing-arg", ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model="offline-missing-arg", base_url="https://fixture.invalid/v1",
            ), api_key="fixture-only",
        )
        app.profiles.select(profile["id"], None)
        responses = [
            _frame("Reading.", [("bad", "read_file", {})]),
            _frame("Finished."),
        ]

        def handler(_request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=responses.pop(0)
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = TaskRuntime(app, client)
        try:
            tid = await runtime.submit({
                "agent_id": agent_id, "objective": "Read without a path",
                "request_id": "missing-path-argument", "workspace": str(project),
            })
            task = runtime.get(tid)
            for _ in range(500):
                task = runtime.get(tid)
                if task["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert task.get("error_code") != "task_runtime_error", task
            # The call was rejected, so the goal is not verified: needs_review is
            # the correct terminal state, not a crash.
            assert task["status"] in {"completed", "needs_review"}, task
            results = [e["data"] for e in runtime.events(tid, limit=200) if e["type"] == "tool_result"]
            assert results, "the rejected call must be recorded as a tool result"
            assert results[0].get("status") != "ok", results
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
