from datetime import UTC, datetime, timedelta


def test_time_and_topic_segments_are_automatic_without_resetting_identity(app, agent_id):
    conversation = app.conversations.create(agent_id, "交流")
    first = app.conversations.append_user(conversation["id"], "开始讨论项目", request_id="first")
    now = datetime(2026, 10, 9, 1, tzinfo=UTC)
    a = app.segments.observe(conversation["id"], first["id"], "项目开发", now=now)
    second = app.conversations.append_user(conversation["id"], "稍后继续", request_id="second")
    same = app.segments.observe(
        conversation["id"], second["id"], "项目开发", now=now + timedelta(minutes=10)
    )
    assert same["id"] == a["id"]
    third = app.conversations.append_user(conversation["id"], "第二天新安排", request_id="third")
    new = app.segments.observe(
        conversation["id"], third["id"], "旅行计划", now=now + timedelta(hours=7)
    )
    assert new["id"] != a["id"]
    assert len(app.segments.list(conversation["id"])) == 2
    assert app.identity.current(agent_id).revision == 0


def test_owner_can_correct_segment_boundaries_without_changing_message_branch(app, agent_id):
    convo = app.conversations.create(agent_id, "交流")
    first = app.conversations.append_user(convo["id"], "第一件事", request_id="first")
    app.segments.observe(convo["id"], first["id"], "原主题")
    second = app.conversations.append_user(convo["id"], "第二件事", request_id="second")
    app.segments.observe(convo["id"], second["id"], "原主题")
    split = app.segments.split(convo["id"], second["id"], "另一主题")
    assert split["title"] == "另一主题"
    assert [m["text"] for m in app.conversations.messages(convo["id"])] == ["第一件事", "第二件事"]
    app.segments.rename(split["id"], "纠正后的主题", expected_revision=split["revision"])
    assert app.segments.list(convo["id"])[1]["title"] == "纠正后的主题"
