import pytest

from lingnexus.common.errors import SourceInvalid


def test_skill_restore_uses_prior_method_and_keeps_revision_history(app, agent_id):
    ref = app.history.add(agent_id, "有效经验", kind="tool", scope="real", request_id="ref")
    item = app.skills.save(agent_id, "方法", "说明", "第一种方法", [ref], request_id="first")
    app.skills.decide(item["id"], "activate", expected_revision=1)
    app.skills.revise(item["id"], "另一种方法", [ref], expected_revision=2, request_id="second")
    restored = app.skills.restore(item["id"], target_revision=2, expected_revision=3)
    assert restored["revision"] == 4
    assert restored["status"] == "active"
    assert restored["instructions"] == "第一种方法"
    assert len(app.skills.versions(item["id"])) == 4


def test_skill_restore_does_not_reactivate_corrected_evidence(app, agent_id):
    ref = app.history.add(agent_id, "错误经验", kind="tool", scope="real", request_id="ref")
    item = app.skills.save(agent_id, "方法", "说明", "方法", [ref], request_id="skill")
    app.skills.decide(item["id"], "activate", expected_revision=1)
    app.history.correct(ref, "证据错误")
    current = app.skills.read(item["id"])
    with pytest.raises(SourceInvalid):
        app.skills.restore(item["id"], target_revision=2, expected_revision=current["revision"])
