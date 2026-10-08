def test_approved_name_evolution_replaces_initial_name_in_current_instructions(app,agent_id):
    ref=app.history.add(agent_id,"用户确认名字可以变化",kind="user",scope="real",request_id="name")
    item=app.identity.propose(agent_id,kind="identity",key="identity.name",text="辰",sources=[ref],base_revision=0,request_id="new-name")
    app.identity.decide(item["id"],"approve",expected_revision=0)
    packet=app.context.build(agent_id,source_refs=[])
    assert "当前名称：辰" in packet.instructions
    assert "你是灵" not in packet.instructions
