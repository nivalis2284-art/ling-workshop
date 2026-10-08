import pytest

from lingnexus.common.errors import PermissionDenied, RevisionConflict, SourceInvalid


def test_edit_user_forks_and_retires_derived_memory(app, agent_id):
    convo = app.conversations.create(agent_id, "日常交流")
    user = app.conversations.append_user(convo["id"], "喜欢咖啡", request_id="coffee")
    answer = app.conversations.append_assistant(
        convo["id"], "记住咖啡", request_id="answer", expected_head=user["id"]
    )
    memory = app.memory.knowledge.save(
        agent_id, "喜欢咖啡", [user["source"]], kind="preference", request_id="pref"
    )
    edited = app.conversations.edit_user(convo["id"], user["id"], "喜欢茶", request_id="tea")
    assert [row["text"] for row in app.conversations.messages(convo["id"])] == ["喜欢茶"]
    with pytest.raises(SourceInvalid):
        app.history.read(answer["source"])
    with pytest.raises(SourceInvalid):
        app.memory.knowledge.read(memory)
    assert app.history.read(user["source"], active_only=False)["text"] == "喜欢咖啡"
    assert edited["id"] != user["id"]


def test_select_old_branch_reactivates_only_selected_path(app, agent_id):
    convo = app.conversations.create(agent_id, "分支")
    user = app.conversations.append_user(convo["id"], "旧问题", request_id="old")
    answer = app.conversations.append_assistant(
        convo["id"], "旧回复", request_id="answer", expected_head=user["id"]
    )
    new = app.conversations.edit_user(convo["id"], user["id"], "新问题", request_id="new")
    app.conversations.select_head(convo["id"], answer["id"])
    assert [row["text"] for row in app.conversations.messages(convo["id"])] == ["旧问题", "旧回复"]
    with pytest.raises(SourceInvalid):
        app.history.read(new["source"])


def test_assistant_cannot_be_directly_edited(app, agent_id):
    convo = app.conversations.create(agent_id, "对话")
    user = app.conversations.append_user(convo["id"], "问题", request_id="user")
    answer = app.conversations.append_assistant(
        convo["id"], "回答", request_id="assistant", expected_head=user["id"]
    )
    with pytest.raises(PermissionDenied):
        app.conversations.edit_user(convo["id"], answer["id"], "伪造回答", request_id="edit")


def test_stale_answer_cannot_attach_after_edit(app, agent_id):
    convo = app.conversations.create(agent_id, "对话")
    user = app.conversations.append_user(convo["id"], "旧问题", request_id="old")
    app.conversations.edit_user(convo["id"], user["id"], "新问题", request_id="new")
    with pytest.raises(RevisionConflict):
        app.conversations.append_assistant(
            convo["id"], "过期回答", request_id="late", expected_head=user["id"]
        )


def test_scene_source_does_not_become_real_memory(app, agent_id):
    convo = app.conversations.create(agent_id, "幻想", scene="island")
    user = app.conversations.append_user(convo["id"], "住在浮空岛", request_id="island")
    assert app.history.read(user["source"])["scope"] == "scene:island"
    with pytest.raises(SourceInvalid):
        app.memory.knowledge.save(
            agent_id, "住在浮空岛", [user["source"]], kind="fact", request_id="fake-real"
        )
