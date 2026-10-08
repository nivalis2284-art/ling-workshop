from pathlib import Path

import pytest


def test_reopen_preserves_identity_and_history(app_factory, tmp_path: Path):
    first = app_factory(tmp_path / "ling.db")
    agent_id = first.create_agent("灵")
    ref = first.history.add(
        agent_id,
        "这是测试经历",
        kind="user",
        scope="test",
        request_id="test-request-1",
    )
    first.close()

    second = app_factory(tmp_path / "ling.db")

    assert second.identity.current(agent_id).agent_id == agent_id
    assert second.memory.read_sources([ref])[0]["text"] == "这是测试经历"


def test_duplicate_request_has_one_record(app, agent_id):
    kwargs = {"kind": "user", "scope": "test", "request_id": "test-request-2"}
    first = app.history.add(agent_id, "内容", **kwargs)
    second = app.history.add(agent_id, "内容", **kwargs)

    assert first == second
    assert app.history.count(agent_id) == 1


def test_duplicate_request_with_changed_content_is_rejected(app, agent_id):
    from lingnexus.common.errors import Conflict

    args = {"kind": "user", "scope": "real", "request_id": "same"}
    app.history.add(agent_id, "原文", **args)
    with pytest.raises(Conflict):
        app.history.add(agent_id, "变化", **args)
    assert app.history.count(agent_id) == 1


def test_transaction_rolls_back_record_and_job(app, agent_id):
    with pytest.raises(RuntimeError), app.database.transaction():
        app.history.add(agent_id, "应当回滚", kind="user", scope="real", request_id="rollback")
        raise RuntimeError("simulated failed operation")
    assert app.history.count(agent_id) == 0
    assert app.history.pending_jobs(agent_id) == []


def test_same_request_can_be_reused_after_failed_transaction(app, agent_id):
    with pytest.raises(RuntimeError), app.database.transaction():
        app.history.add(agent_id, "重试", kind="user", scope="real", request_id="retry")
        raise RuntimeError()
    ref = app.history.add(agent_id, "重试", kind="user", scope="real", request_id="retry")
    assert app.memory.read_sources([ref])[0]["text"] == "重试"
    assert len(app.history.pending_jobs(agent_id)) == 1
