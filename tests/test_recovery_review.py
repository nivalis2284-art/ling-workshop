"""Owner observations preserve uncertain effects; partial applications never replay."""

import asyncio

import pytest
from test_workspace_delivery import _task_workspace

from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.runtime.reconciliation import RecoveryReview


def test_partial_application_reconciliation_only_writes_remaining_files(tmp_path, monkeypatch):
    app, runtime, tid, project, _staged, review = _task_workspace(tmp_path)
    real = app.actions.execute
    calls = []

    def failing(agent, request, name, args):
        calls.append(args["path"])
        if args["path"].endswith("b.txt"):
            raise OSError("after first file")
        return real(agent, request, name, args)

    monkeypatch.setattr(app.actions, "execute", failing)
    try:
        old = asyncio.run(runtime.apply(tid, review_id=review["review_id"]))
        assert old["status"] == "unknown"
        new = asyncio.run(runtime.reconcile(tid, review_id=review["review_id"]))
        assert [f["path"] for f in new["files"]] == ["b.txt", "c.txt"]
        assert asyncio.run(runtime.reconcile(tid, review_id=review["review_id"])) == new
        # Re-create the runtime to prove that the new baseline is durable.
        from lingnexus.tasks import TaskRuntime
        asyncio.run(runtime.aclose())
        runtime = TaskRuntime(app)
        monkeypatch.setattr(app.actions, "execute", real)
        assert asyncio.run(runtime.apply(tid, review_id=new["review_id"]))["status"] == "applied"
        assert (project / "a.txt").read_text() == "new-a"
        assert (project / "b.txt").read_text() == "new-b"
        assert (project / "c.txt").read_text() == "new-c"
        assert runtime.store.get_review(tid, review["review_id"])["status"] == "unknown"
        with pytest.raises(Conflict):
            asyncio.run(runtime.apply(tid, review_id=review["review_id"]))
        assert len(calls) == 2
    finally:
        asyncio.run(runtime.aclose())
        app.close()


def test_reconciliation_refuses_executing_action_without_returned_effect_id(tmp_path, monkeypatch):
    app, runtime, tid, project, _staged, review = _task_workspace(tmp_path)
    try:
        def fail_recording_result(*args, **kwargs):
            raise RuntimeError("write finished but result persistence failed")

        monkeypatch.setattr(app.actions, "_finish", fail_recording_result)
        outcome = asyncio.run(runtime.apply(tid, review_id=review["review_id"]))
        assert outcome["status"] == "unknown"
        assert "effect_id" not in outcome["files"][0]
        assert (project / "a.txt").read_text() == "new-a"
        with pytest.raises(Conflict, match="still executing"):
            asyncio.run(runtime.reconcile(tid, review_id=review["review_id"]))
    finally:
        asyncio.run(runtime.aclose())
        app.close()


@pytest.mark.parametrize("target", ["source", "staged", "unrelated", "revoked"])
def test_reconciliation_refuses_changes_or_revoked_permissions(tmp_path, monkeypatch, target):
    app, runtime, tid, project, staged, review = _task_workspace(tmp_path)
    try:
        def fail(*args, **kwargs):
            raise OSError("unknown write")
        monkeypatch.setattr(app.actions, "execute", fail)
        asyncio.run(runtime.apply(tid, review_id=review["review_id"]))
        if target == "revoked":
            agent = runtime.get(tid)["agent_id"]
            app.permissions.configure(agent, readable=[], writable=[], expected_revision=1)
        else:
            root = staged if target == "staged" else project
            (root / ("extra.txt" if target == "unrelated" else "a.txt")).write_text("external")
        with pytest.raises((Conflict, PermissionError, PermissionDenied)):
            asyncio.run(runtime.reconcile(tid, review_id=review["review_id"]))
    finally:
        asyncio.run(runtime.aclose())
        app.close()


def test_owner_review_does_not_unlock_unknown_or_replay_it(tmp_path):
    app, runtime, tid, _project, _staged, review = _task_workspace(tmp_path)
    try:
        # A nonterminal task can be abandoned without claiming it was cleaned up.
        with app.database.transaction() as db:
            db.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        owner = runtime.sandbox._owner(runtime.get(tid)["agent_id"], tid, "task")
        session = runtime.sandbox.sessions.start(owner, tid, ["private-argument"],
            lambda _cancel, _emit: {"status": "unknown", "error": "cleanup_unconfirmed"})
        asyncio.run(runtime.sandbox.sessions.wait_run(owner, tid))
        reviews = RecoveryReview(app, None, runtime)
        view = reviews.view(tid, "task")
        assert "private-argument" not in str(view)
        args = {"request_id": "owner-check-1", "expected_version": view["version"],
                "conclusion": "reviewed", "note": "Manually checked the output; process is uncertain"}
        result = reviews.record(tid, "task", **args)
        assert result["reviews"][0]["cleanup_confirmed"] is False
        assert reviews.record(tid, "task", **args) == result
        with pytest.raises(Conflict):
            asyncio.run(runtime.resume(tid))
        with pytest.raises(Conflict):
            reviews.record(tid, "task", **(args | {"request_id": "stale"}))
        result = reviews.record(tid, "task", request_id="abandon", expected_version=result["version"],
                                conclusion="abandon", note="Restart separately")
        assert result["status"] == "cancelled"
        assert runtime.sandbox.sessions.poll(session["session_id"], owner)["status"] == "unknown"
        with pytest.raises(Conflict):
            asyncio.run(runtime.apply(tid, review_id=review["review_id"]))
    finally:
        asyncio.run(runtime.aclose())
        app.close()


def test_recovery_http_boundary_requires_owner_and_rejects_forged_confirmation(client, headers):
    from lingnexus.models import APIFamily, ModelProfile

    app = client.app.state.services
    agent = app.create_agent("owner boundary fixture")
    profile = app.profiles.save("fixture", ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture", base_url="https://fixture.invalid"))
    app.profiles.select(profile["id"], None)
    response = client.post("/api/tasks", headers=headers, json={"agent_id": agent,
        "objective": "No credentials, do not call a model", "request_id": "owner-review-http"})
    assert response.status_code == 202
    tid = response.json()["id"]
    endpoint = "/api/execution/recovery/task/" + tid
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={**headers, "Origin": "https://attacker.invalid"}).status_code == 403
    view = client.get(endpoint, headers=headers).json()
    request = {"request_id": "http-note", "expected_version": view["version"],
               "conclusion": "reviewed", "note": "Inspected recorded evidence"}
    assert client.post(endpoint, headers=headers, json={**request, "cleanup_confirmed": True}).status_code == 422
    assert client.post(endpoint, headers=headers, json={**request, "expected_version": "0"*64}).status_code == 409
    assert client.post("/api/tasks/"+tid+"/reconcile", json={"review_id": "foreign"}).status_code == 401
    assert client.get("/api/tasks/"+tid+"/application", headers=headers).json() is None
    assert client.get(endpoint+"-missing", headers=headers).status_code == 404


def test_chat_owner_review_is_replayable_and_abandonment_blocks_recovery(app, agent_id):
    from test_run_recovery import _configure, _interrupt_run

    from lingnexus.runtime import ChatRuntime

    _configure(app)
    run_id = _interrupt_run(app, agent_id, "owner-review-chat")
    runtime = ChatRuntime(app)
    try:
        reviews = RecoveryReview(app, runtime, None)
        view = reviews.view(run_id, "chat")
        result = reviews.record(run_id, "chat", request_id="abandon-chat", expected_version=view["version"],
                                conclusion="abandon", note="Start in a separate workspace")
        assert result["status"] == "cancelled"
        assert result["reviews"][0]["cleanup_confirmed"] is False
        events, _ = runtime.store.read_events(run_id, 0)
        recorded = events[-1]
        assert recorded["event"] == "notice"
        assert recorded["data"]["run_id"] == run_id
        assert recorded["data"]["code"] == "owner_recovery_review"
        with pytest.raises(Conflict):
            runtime.recover(run_id)
    finally:
        asyncio.run(runtime.aclose())
