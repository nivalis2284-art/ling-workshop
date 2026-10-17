"""The owner can inspect local budget effects without a model connection."""

from test_work_events import closed_event


def test_context_preview_uses_no_credentials_or_new_run(client, headers):
    app = client.app.state.services
    aid = app.create_agent("预览")
    cid, event, _ = closed_event(app, aid)
    app.profiles.clear_credentials()
    with app.database.transaction() as db:
        before = db.execute("SELECT count(*) FROM chat_runs").fetchone()[0]
    response = client.get(f"/api/conversations/{cid}/context-preview", headers=headers)
    assert response.status_code == 200
    preview = response.json()
    assert preview["available"] is True
    assert preview["estimated_total_with_output"] == preview["manifest"]["estimated_input_tokens"] + preview["output_reserve"] + preview["tool_schema_estimate"]
    assert preview["manifest"]["work_events"][0]["sent"] is True
    assert preview["manifest"]["work_events"][0]["event_id"] == event["id"]
    assert preview["manifest"]["work_events"][0]["replaced_messages"] == 2
    assert "不是供应商" in preview["note"]
    with app.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM chat_runs").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM event_summary_jobs").fetchone()[0] == 0
    assert client.get(f"/api/conversations/{cid}/context-preview").status_code == 401
    # Explicitly requesting a disabled optional summary fails without a job or
    # a model call; closing the event never schedules one implicitly.
    assert client.post(f"/api/work-events/{event['id']}/summarize", headers=headers, json={"expected_revision": event["revision"]}).status_code == 409
