import pytest

from lingnexus.common.errors import SourceInvalid


def test_skill_candidate_cannot_be_loaded_as_effective_until_owner_activates(app, agent_id):
    ref = app.history.add(agent_id, "任务过程记录", kind="tool", scope="real", request_id="task")
    item = app.skills.save(
        agent_id,
        "复查资料",
        "核对来源的办法",
        "先查询原文，再核对日期。",
        [ref],
        request_id="skill",
    )
    assert item["status"] == "candidate"
    assert app.skills.available(agent_id) == []
    app.skills.decide(item["id"], "activate", expected_revision=1)
    assert app.skills.available(agent_id)[0]["name"] == "复查资料"
    assert app.skills.read(item["id"])["instructions"].startswith("先查询原文")


def test_skill_update_preserves_older_versions_and_requires_new_review(app, agent_id):
    ref = app.history.add(agent_id, "依据", kind="tool", scope="real", request_id="evidence")
    item = app.skills.save(agent_id, "技巧", "描述", "第一方法", [ref], request_id="v1")
    app.skills.decide(item["id"], "activate", expected_revision=1)
    new = app.skills.revise(item["id"], "改进方法", [ref], expected_revision=2, request_id="v2")
    assert new["status"] == "candidate"
    assert new["revision"] == 3
    assert app.skills.available(agent_id) == []
    assert len(app.skills.versions(item["id"])) == 3





def test_skill_cannot_grant_tools_or_override_owner_permissions(app, agent_id):
    ref = app.history.add(agent_id, "依据", kind="tool", scope="real", request_id="evidence")
    item = app.skills.save(agent_id, "方法", "说明", "可以提出工作方法。", [ref], request_id="save")
    app.skills.decide(item["id"], "activate", expected_revision=1)
    assert app.permissions.current(agent_id)["writable"] == []
