import pytest

from lingnexus.common.errors import RevisionConflict, SourceInvalid


def source(app, agent_id, text="观察依据"):
    return app.history.add(agent_id, text, kind="user", scope="real", request_id=text)


def test_knowledge_preserves_source_and_correction_retires_fact(app, agent_id):
    ref = source(app, agent_id)
    item = app.memory.knowledge.save(
        agent_id, "偏好先看概览", [ref], kind="preference", request_id="fact"
    )
    assert app.memory.knowledge.search(agent_id, "概览")[0]["id"] == item
    app.history.correct(ref, "新偏好还未确认")
    assert app.memory.knowledge.search(agent_id, "概览") == []
    with pytest.raises(SourceInvalid):
        app.memory.knowledge.read(item)


def test_proposed_goal_not_activated_by_background_progress(app, agent_id):
    ref = source(app, agent_id)
    item = app.development.propose_goal(agent_id, "学习新的领域", [ref], request_id="goal")
    assert item["status"] == "proposed"
    with pytest.raises(RevisionConflict):
        app.development.update_progress(item["id"], "开始做事", expected_revision=1)
    active = app.development.decide(item["id"], "activate", expected_revision=1)
    assert active["status"] == "active"
    progressed = app.development.update_progress(item["id"], "完成资料梳理", expected_revision=2)
    assert progressed["revision"] == 3
    assert progressed["progress"] == "完成资料梳理"


def test_goal_revocation_and_version_checks_survive_restart(app, agent_id, app_factory):
    ref = source(app, agent_id)
    goal = app.development.propose_goal(agent_id, "关注资料", [ref], request_id="g")
    app.development.decide(goal["id"], "activate", expected_revision=1)
    app.development.decide(goal["id"], "pause", expected_revision=2)
    reopened = app_factory(app.database.path)
    assert reopened.development.read(goal["id"])["status"] == "paused"
    with pytest.raises(RevisionConflict):
        reopened.development.update_progress(goal["id"], "过期后台工作", expected_revision=2)


def test_export_restores_knowledge_and_goal_history(app, agent_id, tmp_path, app_factory):
    ref = source(app, agent_id)
    kid = app.memory.knowledge.save(agent_id, "知识", [ref], kind="fact", request_id="k")
    goal = app.development.propose_goal(agent_id, "目标", [ref], request_id="g")
    out = tmp_path / "export"
    app.maintenance.export_to(out)
    app.maintenance.restore_from(out, tmp_path / "restored.db")
    other = app_factory(tmp_path / "restored.db")
    assert other.memory.knowledge.read(kid)["statement"] == "知识"
    assert other.development.read(goal["id"])["status"] == "proposed"
