from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime.payload import build_payload


def test_long_history_is_bounded_and_older_raw_detail_remains_readable(app, agent_id):
    refs = [
        app.history.add(
            agent_id,
            "旧交流" + str(i) + ":" + "重复过程" * 90,
            kind="user" if i % 2 == 0 else "assistant",
            scope="real",
            request_id=str(i),
        )
        for i in range(20)
    ]
    newest = app.history.add(
        agent_id, "继续当前问题", kind="user", scope="real", request_id="latest"
    )
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="test",
        base_url="https://example.test",
        context_window_tokens=8000,
        max_output_tokens=1000,
    )
    payload, manifest = build_payload(app, agent_id, profile, [*refs, newest], scope="real")
    assert payload["messages"][-1]["content"] == "继续当前问题"
    assert manifest["omitted"]
    assert len(str(payload).encode()) < 8000
    assert app.history.read(refs[0])["text"].startswith("旧交流0:")


def test_relevant_old_memory_is_recalled_with_precise_source(app, agent_id):
    old = app.history.add(
        agent_id, "复查约定：周五15:30，地点东楼。", kind="user", scope="real", request_id="old"
    )
    app.memory.save_episode(
        agent_id, "复查约定", "约定周五复查，时间和地点见原文", [old], request_id="old-episode"
    )
    current = app.history.add(
        agent_id, "复查约定的地点是什么？", kind="user", scope="real", request_id="query"
    )
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_RESPONSES, model="test", base_url="https://example.test"
    )
    payload, manifest = build_payload(app, agent_id, profile, [current], scope="real")
    assert "东楼" in str(payload)
    assert old.model_dump() in manifest["recall_sources"]


def test_deselected_branch_memory_does_not_return_to_context(app, agent_id):
    old = app.history.add(agent_id, "咖啡偏好", kind="user", scope="real", request_id="coffee")
    app.memory.save_episode(agent_id, "咖啡偏好", "喜欢咖啡", [old], request_id="memory")
    app.history.set_visible(old, False)
    current = app.history.add(agent_id, "咖啡偏好？", kind="user", scope="real", request_id="query")
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="test", base_url="https://example.test"
    )
    payload, manifest = build_payload(app, agent_id, profile, [current], scope="real")
    assert "喜欢咖啡" not in str(payload)
    assert manifest["recall_sources"] == []
