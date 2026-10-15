import sys
import threading
import time

import pytest

from lingnexus.tools._process import run_process
from lingnexus.tools.sessions import CommandSessions


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def test_output_is_incremental_bounded_and_owner_scoped():
    sessions = CommandSessions(max_output_bytes=5)
    started = threading.Event()
    finish = threading.Event()

    def runner(cancel_event, emit_output):
        emit_output("你你")
        started.set()
        finish.wait(2)
        return {"status": "success", "exit_code": 0, "stdout": "duplicate"}

    started_session = sessions.start("agent/run-a", "run-a", ["python", "-c", "pass"], runner)
    session_id = started_session["session_id"]
    assert started.wait(1)

    first = sessions.poll(session_id, "agent/run-a")
    assert first["output"] == "你"
    assert first["cursor"] == 1
    assert first["status"] == "running"
    assert first["output_truncated"] is True
    assert "argv" not in first and "owner_id" not in first and "run_id" not in first
    assert "stdout" not in (first["result"] or {})
    with pytest.raises(PermissionError):
        sessions.poll(session_id, "agent/run-b")
    with pytest.raises(PermissionError):
        sessions.cancel(session_id, "agent/run-b")

    incremental = sessions.poll(session_id, "agent/run-a", after=first["cursor"])
    assert incremental["output"] == ""
    assert incremental["cursor"] == first["cursor"]

    finish.set()
    done = wait_for(lambda: _terminal_poll(sessions, session_id, "agent/run-a"))
    assert done["status"] == "success"


def _terminal_poll(sessions, session_id, owner_id):
    value = sessions.poll(session_id, owner_id)
    terminal = {"success", "failed", "timeout", "cancelled", "unknown"}
    return value if value["status"] in terminal else None


def test_run_process_cancellation_reaps_owned_python_child_and_streams_output():
    cancel_event = threading.Event()
    emitted = []
    ready = threading.Event()
    result_box = []

    def on_output(text):
        emitted.append(text)
        if "ready" in "".join(emitted):
            ready.set()

    def run():
        result_box.append(run_process(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            timeout=20,
            max_output_bytes=4096,
            new_session=True,
            cancel_event=cancel_event,
            on_output=on_output,
        ))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert ready.wait(3), "Python child failed to write its marker"
    cancel_event.set()
    worker.join(4)

    assert not worker.is_alive()
    assert result_box
    assert result_box[0].cancelled is True
    assert result_box[0].timed_out is False
    assert b"ready" in result_box[0].stdout


def test_session_cancel_uses_process_event_and_reports_confirmed_cancel():
    sessions = CommandSessions(total_timeout=10)

    def runner(cancel_event, emit_output):
        result = run_process(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            timeout=20,
            max_output_bytes=4096,
            new_session=True,
            cancel_event=cancel_event,
            on_output=emit_output,
        )
        if result.cancelled:
            return {"status": "cancelled", "cancelled": True, "exit_code": result.returncode}
        return {"status": "success" if result.ok else "failed", "exit_code": result.returncode}

    session = sessions.start("agent/run-c", "run-c", [sys.executable, "-c", "pass"], runner)
    session_id = session["session_id"]
    wait_for(lambda: "ready" in sessions.poll(session_id, "agent/run-c")["output"])
    assert sessions.cancel(session_id, "agent/run-c")["status"] == "cancelling"
    done = wait_for(lambda: _terminal_poll(sessions, session_id, "agent/run-c"))
    assert done["status"] == "cancelled"
    assert done["result"]["exit_code"] is not None


def test_per_run_limit_deadline_and_unconfirmed_cancel_are_explicit():
    sessions = CommandSessions(max_sessions=1, total_timeout=0.08)
    release = threading.Event()

    def ignores_cancel(cancel_event, emit_output):
        release.wait(1)
        return {"status": "success", "exit_code": 0}

    first = sessions.start("agent/run-d", "run-d", ["fake"], ignores_cancel)
    with pytest.raises(RuntimeError, match="run_session_limit_reached"):
        sessions.start("agent/run-d", "run-d", ["fake"], ignores_cancel)
    deadline_state = wait_for(
        lambda: sessions.poll(first["session_id"], "agent/run-d")["status"] == "cancelling"
    )
    assert deadline_state is True
    release.set()
    done = wait_for(lambda: _terminal_poll(sessions, first["session_id"], "agent/run-d"))
    assert done["status"] == "unknown"
    assert done["result"]["error"] == "timeout_cleanup_unconfirmed"


def test_restart_marks_running_unknown_without_rerunning():
    saved = []
    release = threading.Event()

    def save(record):
        saved[:] = [dict(record)]

    sessions = CommandSessions(load=lambda: (), save=save)

    def runner(cancel_event, emit_output):
        emit_output("still running")
        release.wait(2)
        return {"status": "success"}

    started = sessions.start("agent/run-e", "run-e", ["secret-command"], runner)
    wait_for(lambda: saved and saved[0]["output"] == "still running")
    sessions.close()

    recovered = CommandSessions(load=lambda: saved, save=save)
    result = recovered.poll(started["session_id"], "agent/run-e")
    release.set()
    assert result["status"] == "unknown"
    assert result["error"] == "process_state_after_restart"
    assert result["output"] == "still running"
    assert "argv" not in result
def test_product_session_scope_and_revocation(app, agent_id, tmp_path):
    import asyncio
    import sys

    from lingnexus.common.errors import PermissionDenied

    async def exercise():
        root = tmp_path / "commands"
        root.mkdir()
        app.permissions.configure(agent_id, readable=[root], writable=[root], expected_revision=0)
        result = await app.sandbox.command(agent_id, "run-one", "chat",
                                           [sys.executable, "-u", "-c", "import time;print('ready');time.sleep(60)"],
                                           "work", root, long_running=True)
        session = result["execution"]["session_id"]
        assert result["status"] == "running"
        try:
            app.sandbox.observe(session, agent_id, "other-run", "chat")
        except PermissionError:
            pass
        else:
            raise AssertionError("a session leaked across runs")
        grants = app.permissions.current(agent_id)
        app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=grants["revision"])
        owner = app.sandbox._owner(agent_id, "run-one", "chat")
        for _ in range(80):
            view = app.sandbox.sessions.poll(session, owner)
            if view["status"] not in {"running", "cancelling"}:
                break
            await asyncio.sleep(0.05)
        assert view["status"] == "cancelled", view
        assert view["result"]["cleanup_confirmed"] is True
        try:
            app.sandbox.observe(session, agent_id, "run-one", "chat")
        except PermissionDenied:
            pass
        else:
            raise AssertionError("revoked output was returned")
        await app.sandbox.sessions.aclose()

    asyncio.run(exercise())


def test_session_api_requires_owner_and_valid_run(client, headers):
    assert client.get("/execution/sessions/missing?run_id=missing").status_code == 401
    assert client.get("/execution/sessions/missing?run_id=missing", headers=headers).status_code == 404
    assert client.get("/execution/sessions/missing?run_id=missing&after=-1", headers=headers).status_code == 422


def test_long_running_product_command_exceeds_old_thirty_second_limit(app, agent_id, tmp_path):
    import asyncio
    import sys
    import time

    async def exercise():
        root = tmp_path / "long-command"
        root.mkdir()
        app.permissions.configure(agent_id, readable=[root], writable=[root], expected_revision=0)
        started = time.monotonic()
        result = await app.sandbox.command(agent_id, "slow-run", "chat",
                                           [sys.executable, "-u", "-c",
                                            "import time;print('before');time.sleep(31);print('after')"],
                                           "verify", root, long_running=True)
        session = result["execution"]["session_id"]
        output, cursor = result["stdout"], result["execution"]["cursor"]
        for _ in range(90):
            if result["status"] not in {"running", "cancelling"}:
                break
            await asyncio.sleep(0.5)
            result = app.sandbox.observe(session, agent_id, "slow-run", "chat", cursor)
            output += result["stdout"]
            cursor = result["execution"]["cursor"]
        assert result["status"] == "success", result
        assert result["verification_passed"] is True
        assert time.monotonic() - started > 30
        assert output.count("before") == 1 and output.count("after") == 1
        await app.sandbox.sessions.aclose()

    asyncio.run(exercise())
