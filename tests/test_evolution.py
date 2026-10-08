import pytest

from lingnexus.common.errors import Conflict, RevisionConflict, SourceInvalid
from lingnexus.common.types import SourceRef


def proposal(
    app, agent_id, *, kind="identity", key="identity.description", text="喜欢探索", source=None
):
    ref = source or app.history.add(agent_id, text, kind="user", scope="real", request_id=text)
    return app.identity.propose(
        agent_id,
        kind=kind,
        key=key,
        text=text,
        sources=[ref],
        base_revision=app.identity.current(agent_id).revision,
        request_id="p-" + text,
    )


def test_protected_state_waits_for_owner_and_survives_restart(app, agent_id, app_factory):
    item = proposal(app, agent_id)
    assert item["status"] == "awaiting_owner"
    assert app.identity.current(agent_id).entries == {}
    app.identity.decide(item["id"], "approve", expected_revision=0)
    reopened = app_factory(app.database.path)
    assert reopened.identity.current(agent_id).entries == {"identity.description": "喜欢探索"}
    assert reopened.identity.current(agent_id).revision == 1


def test_cannot_disguise_identity_change_as_a_habit(app, agent_id):
    item = proposal(app, agent_id, kind="habit", key="identity.description")
    assert item["status"] == "awaiting_owner"
    assert not app.identity.current(agent_id).entries


def test_low_risk_structured_preference_can_apply_automatically(app, agent_id):
    item = proposal(
        app, agent_id, kind="habit", key="communication.response_length", text="concise"
    )
    assert item["status"] == "applied_auto"
    assert app.identity.current(agent_id).entries["communication.response_length"] == "concise"


def test_arbitrary_habit_text_requires_review_without_semantic_validator(app, agent_id):
    item = proposal(
        app, agent_id, kind="habit", key="communication.response_length", text="改变身份"
    )
    assert item["status"] == "awaiting_owner"


def test_stale_proposal_cannot_overwrite_newer_state(app, agent_id):
    a = proposal(app, agent_id, text="最初认识")
    b = proposal(app, agent_id, text="另一认识")
    app.identity.decide(b["id"], "approve", expected_revision=0)
    with pytest.raises(RevisionConflict):
        app.identity.decide(a["id"], "approve", expected_revision=0)
    assert app.identity.current(agent_id).entries["identity.description"] == "另一认识"


def test_rejection_is_idempotent_and_cannot_later_apply(app, agent_id):
    item = proposal(app, agent_id)
    app.identity.decide(item["id"], "reject", expected_revision=0)
    assert app.identity.decide(item["id"], "reject", expected_revision=0)["status"] == "rejected"
    with pytest.raises(Conflict):
        app.identity.decide(item["id"], "approve", expected_revision=0)
    assert app.identity.current(agent_id).entries == {}


def test_correction_removes_derived_identity_but_keeps_independent_growth(app, agent_id):
    a = proposal(app, agent_id, text="旧认识")
    app.identity.decide(a["id"], "approve", expected_revision=0)
    proposal(app, agent_id, kind="habit", key="communication.response_length", text="concise")
    ref = SourceRef(**a["sources"][0])
    new = app.history.correct(ref, "旧认识错误")
    assert new.revision == 2
    assert app.identity.current(agent_id).entries == {"communication.response_length": "concise"}
    assert app.history.read(ref, active_only=False)["text"] == "旧认识"
    with pytest.raises(SourceInvalid):
        app.history.read(ref)


def test_pending_proposal_cannot_apply_after_source_correction(app, agent_id):
    item = proposal(app, agent_id)
    app.history.correct(SourceRef(**item["sources"][0]), "已经纠正")
    with pytest.raises(SourceInvalid):
        app.identity.decide(item["id"], "approve", expected_revision=0)


def test_scene_cannot_become_real_identity(app, agent_id):
    ref = app.history.add(
        agent_id, "我是龙", kind="user", scope="scene:fantasy", request_id="fantasy"
    )
    with pytest.raises(SourceInvalid):
        proposal(app, agent_id, source=ref)


def test_episode_summary_keeps_original_detail_and_invalidates_on_correction(app, agent_id):
    ref = app.history.add(
        agent_id, "周五15:30去复查", kind="user", scope="real", request_id="appointment"
    )
    episode = app.memory.save_episode(agent_id, "复查", "约定了复查", [ref], request_id="episode")
    assert "15:30" in app.memory.read_sources([ref])[0]["text"]
    assert app.memory.read_episode(episode)["summary"] == "约定了复查"
    app.history.correct(ref, "改到下周")
    with pytest.raises(SourceInvalid):
        app.memory.read_episode(episode)
    assert app.memory.find_episodes(agent_id, "复查") == []


def test_two_character_chinese_search_and_scope_filter(app, agent_id):
    real = app.history.add(agent_id, "想喝咖啡", kind="user", scope="real", request_id="coffee")
    fake = app.history.add(agent_id, "测试咖啡", kind="user", scope="test", request_id="fakecoffee")
    eid = app.memory.save_episode(
        agent_id, "午后咖啡", "想喝咖啡", [real], request_id="real-episode"
    )
    app.memory.save_episode(
        agent_id, "测试咖啡", "测试", [fake], request_id="fake-episode", scope="test"
    )
    assert [row["id"] for row in app.memory.find_episodes(agent_id, "咖啡")] == [eid]


def test_episode_request_conflict_does_not_erase_prior_card(app, agent_id):
    ref = app.history.add(agent_id, "证据", kind="user", scope="real", request_id="evidence")
    eid = app.memory.save_episode(agent_id, "标题", "原摘要", [ref], request_id="once")
    with pytest.raises(Conflict):
        app.memory.save_episode(agent_id, "标题", "新摘要", [ref], request_id="once")
    assert app.memory.read_episode(eid)["summary"] == "原摘要"


def test_rollback_is_new_revision_with_prior_state_and_provenance(app, agent_id):
    a = proposal(app, agent_id, text="最初的认识")
    app.identity.decide(a["id"], "approve", expected_revision=0)
    b = proposal(app, agent_id, text="新的认识")
    app.identity.decide(b["id"], "approve", expected_revision=1)
    restored = app.identity.rollback(agent_id, target_revision=1, expected_revision=2)
    assert restored.revision == 3
    assert restored.entries["identity.description"] == "最初的认识"
    app.history.correct(SourceRef(**a["sources"][0]), "最初认识已失效")
    assert "identity.description" not in app.identity.current(agent_id).entries


def test_rollback_cannot_resurrect_invalid_source(app, agent_id):
    item = proposal(app, agent_id)
    app.identity.decide(item["id"], "approve", expected_revision=0)
    app.history.correct(SourceRef(**item["sources"][0]), "该依据失效")
    now = app.identity.current(agent_id).revision
    with pytest.raises(SourceInvalid):
        app.identity.rollback(agent_id, target_revision=1, expected_revision=now)
    assert app.identity.current(agent_id).entries == {}


def test_owner_can_reject_old_pending_proposal_after_other_growth(app, agent_id):
    older = proposal(app, agent_id, text="旧候选")
    newer = proposal(app, agent_id, text="有效变化")
    app.identity.decide(newer["id"], "approve", expected_revision=0)
    result = app.identity.decide(older["id"], "reject", expected_revision=1)
    assert result["status"] == "rejected"
    assert app.identity.current(agent_id).revision == 1
    assert app.identity.current(agent_id).entries == {"identity.description": "有效变化"}
