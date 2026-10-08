def test_recall_deduplicates_same_source_and_covers_multiple_events(app, agent_id):
    from lingnexus.context.recall import candidates

    a = app.history.add(
        agent_id, "楠桥项目预算1760，负责人顾宁", kind="user", scope="real", request_id="a"
    )
    b = app.history.add(
        agent_id, "溪站项目预算1280，负责人韩川", kind="user", scope="real", request_id="b"
    )
    app.memory.save_episode(agent_id, "楠桥项目", "预算1760", [a], request_id="a-e")
    app.memory.knowledge.save(agent_id, "楠桥项目预算1760", [a], kind="fact", request_id="a-k")
    app.memory.save_episode(agent_id, "楠桥迭代", "负责人顾宁", [a], request_id="a-e2")
    app.memory.save_episode(agent_id, "溪站项目", "预算1280", [b], request_id="b-e")
    text, refs, markers = candidates(app, agent_id, "楠桥 溪站：分别预算多少？", "real")
    assert "1760" in text and "1280" in text
    assert {r.id for r in refs} == {a.id, b.id}
    assert len(markers) == 2


def test_recent_filler_does_not_displace_relevant_old_memory(app, agent_id):
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.runtime.payload import build_payload

    old = app.history.add(
        agent_id, "楠桥项目的会议房间是西楼B312。", kind="user", scope="real", request_id="old"
    )
    app.memory.save_episode(agent_id, "楠桥项目会议", "房间见原文", [old], request_id="event")
    filler = [
        app.history.add(agent_id, "不相关过程" * 120, kind="user", scope="real", request_id=str(i))
        for i in range(20)
    ]
    query = app.history.add(
        agent_id, "楠桥项目的会议房间是什么？", kind="user", scope="real", request_id="query"
    )
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="test",
        base_url="https://example.test",
        context_window_tokens=6000,
        max_output_tokens=1000,
    )
    payload, manifest = build_payload(app, agent_id, profile, [*filler, query], scope="real")
    assert "B312" in str(payload)
    assert old.model_dump() in manifest["recall_sources"]
