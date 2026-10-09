import pytest

from lingnexus.common.errors import NotFound, PermissionDenied, RevisionConflict, SourceInvalid


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


def _turn(app, convo_id, text, request_id):
    user = app.conversations.append_user(convo_id, text, request_id=request_id)
    answer = app.conversations.append_assistant(
        convo_id, "回复" + text, request_id=request_id + "-a", expected_head=user["id"]
    )
    return user, answer


def test_delete_turn_removes_round_and_retires_derived_memory(app, agent_id):
    convo = app.conversations.create(agent_id, "日常交流")
    first, first_reply = _turn(app, convo["id"], "你好", "u1")
    second, _ = _turn(app, convo["id"], "喜欢咖啡吗", "u2")
    memory = app.memory.knowledge.save(
        agent_id, "用户喜欢咖啡", [second["source"]], kind="preference", request_id="pref"
    )
    app.conversations.delete_turns(convo["id"], [second["id"]])
    assert [row["text"] for row in app.conversations.messages(convo["id"])] == [
        "你好",
        "回复你好",
    ]
    with pytest.raises(SourceInvalid):
        app.history.read(second["source"])
    with pytest.raises(SourceInvalid):
        app.memory.knowledge.read(memory)
    assert app.history.read(first_reply["source"])["text"] == "回复你好"
    with pytest.raises(NotFound):
        app.conversations.delete_turns(convo["id"], [second["id"]])
    third = app.conversations.append_user(convo["id"], "再见", request_id="u3")
    assert third["parent_id"] == first_reply["id"]


def test_delete_multiple_nonadjacent_turns_relinks_chain(app, agent_id):
    convo = app.conversations.create(agent_id, "多轮")
    _turn(app, convo["id"], "一", "u1")
    u2, _ = _turn(app, convo["id"], "二", "u2")
    u3, _ = _turn(app, convo["id"], "三", "u3")
    _turn(app, convo["id"], "四", "u4")
    app.conversations.delete_turns(convo["id"], [u2["id"], u3["id"]])
    assert [row["text"] for row in app.conversations.messages(convo["id"])] == [
        "一",
        "回复一",
        "四",
        "回复四",
    ]


def test_delete_turn_drops_old_branches_below_it(app, agent_id):
    convo = app.conversations.create(agent_id, "分支删除")
    u1, _ = _turn(app, convo["id"], "一", "u1")
    u2, a2 = _turn(app, convo["id"], "二", "u2")
    _turn(app, convo["id"], "三", "u3")
    alt = app.conversations.edit_user(convo["id"], u2["id"], "二改", request_id="edit2")
    app.conversations.select_head(convo["id"], a2["id"])
    app.conversations.delete_turns(convo["id"], [u2["id"]])
    remaining = [row["text"] for row in app.conversations.messages(convo["id"])]
    assert remaining == ["一", "回复一"]
    # An edited version hangs off the previous message, so it survives as a dead
    # node; what matters is that it left the path and its source is retired.
    path_ids = [row["id"] for row in app.conversations.messages(convo["id"])]
    assert alt["id"] not in path_ids
    with pytest.raises(SourceInvalid):
        app.history.read(alt["source"])
    tail = app.conversations.append_user(convo["id"], "新话", request_id="u4")
    assert tail["parent_id"] == app.conversations.messages(convo["id"])[-2]["id"]


def test_delete_all_turns_empties_conversation(app, agent_id):
    convo = app.conversations.create(agent_id, "清空")
    u1, _ = _turn(app, convo["id"], "一", "u1")
    u2, _ = _turn(app, convo["id"], "二", "u2")
    app.conversations.delete_turns(convo["id"], [u1["id"], u2["id"]])
    assert app.conversations.messages(convo["id"]) == []
    fresh = app.conversations.append_user(convo["id"], "重新开始", request_id="fresh")
    assert fresh["parent_id"] is None


def test_delete_turns_rejects_assistant_anchor(app, agent_id):
    convo = app.conversations.create(agent_id, "拒绝")
    _, answer = _turn(app, convo["id"], "一", "u1")
    with pytest.raises(PermissionDenied):
        app.conversations.delete_turns(convo["id"], [answer["id"]])
