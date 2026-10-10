import pytest

from lingnexus.common.errors import ContextTooLarge


def test_approved_growth_reaches_context_without_pending_claim(app, agent_id):
    ref = app.history.add(agent_id, "学习后的认识", kind="user", scope="real", request_id="growth")
    p = app.identity.propose(
        agent_id,
        kind="identity",
        key="identity.description",
        text="我愿意核实后表达",
        sources=[ref],
        base_revision=0,
        request_id="growth-p",
    )
    before = app.context.build(agent_id, source_refs=[])
    assert "我愿意核实后表达" not in before.instructions
    app.identity.decide(p["id"], "approve", expected_revision=0)
    after = app.context.build(agent_id, source_refs=[ref])
    assert "我愿意核实后表达" in after.instructions
    assert after.manifest["self_revision"] == 1
    assert after.evidence[0]["id"] == ref.id
    assert before.instructions != after.instructions


def test_same_state_has_stable_prefix(app, agent_id):
    first = app.context.build(agent_id, source_refs=[])
    second = app.context.build(agent_id, source_refs=[])
    assert first.instructions == second.instructions
    assert first.manifest["token_count_exact"] is False


def test_required_context_is_never_silently_truncated(app, agent_id):
    from lingnexus.context.builder import BudgetPolicy

    with pytest.raises(ContextTooLarge):
        app.context.build(
            agent_id,
            source_refs=[],
            budget=BudgetPolicy(context_limit=20, output_reserve=10, tool_reserve=5),
        )


def test_long_evidence_is_omitted_with_readable_reference(app, agent_id):
    from lingnexus.context.builder import BudgetPolicy

    ref = app.history.add(
        agent_id, "很长的工具输出" * 400, kind="tool", scope="real", request_id="long"
    )
    result = app.context.build(
        agent_id,
        source_refs=[ref],
        budget=BudgetPolicy(context_limit=2000, output_reserve=300, tool_reserve=200),
    )
    assert not result.evidence
    assert result.manifest["omitted"][0]["id"] == ref.id
    assert len(app.memory.read_sources([ref])[0]["text"]) > 2000
