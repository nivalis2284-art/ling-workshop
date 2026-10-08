def test_agent_can_read_exact_source_detail_beyond_a_summary(app, agent_id):
    ref = app.history.add(
        agent_id,
        "原文第一个细节：17；第二个细节：25。",
        kind="user",
        scope="real",
        request_id="original",
    )
    result = app.actions.execute(
        agent_id,
        "recall",
        "read_source",
        {"source_id": ref.id, "revision": 1, "offset": 0, "limit": 200},
    )
    assert result["status"] == "completed"
    assert "25" in result["result"]["text"]
    assert result["result"]["source"]["revision"] == 1
