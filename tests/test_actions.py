import pytest

from lingnexus.common.errors import Conflict


def test_replayed_file_create_returns_result_without_repeating_effect(app, agent_id, tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
    result = app.actions.execute(
        agent_id, "write-1", "create_file", {"path": str(folder / "result.txt"), "text": "产物"}
    )
    assert result["status"] == "completed"
    (folder / "result.txt").write_text("后来修改", encoding="utf-8")
    replay = app.actions.execute(
        agent_id, "write-1", "create_file", {"path": str(folder / "result.txt"), "text": "产物"}
    )
    assert replay == result
    assert (folder / "result.txt").read_text(encoding="utf-8") == "后来修改"


def test_action_id_rejects_changed_parameters(app, agent_id, tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
    app.actions.execute(
        agent_id, "same", "create_file", {"path": str(folder / "a.txt"), "text": "a"}
    )
    with pytest.raises(Conflict):
        app.actions.execute(
            agent_id, "same", "create_file", {"path": str(folder / "b.txt"), "text": "b"}
        )
    assert not (folder / "b.txt").exists()


def test_unauthorized_file_action_has_no_effect(app, agent_id, tmp_path):
    result = app.actions.execute(
        agent_id, "denied", "create_file", {"path": str(tmp_path / "out.txt"), "text": "no"}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "permission_denied"
    assert not (tmp_path / "out.txt").exists()


def test_unknown_effect_is_not_replayed_after_crash(app, agent_id, tmp_path, app_factory):
    folder = tmp_path / "work"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
    args = {"path": str(folder / "file.txt"), "text": "potential effect"}
    intent = app.actions.prepare(agent_id, "interrupted", "create_file", args)
    assert intent["status"] == "prepared"
    app.actions.mark_executing(intent["id"])
    reopened = app_factory(app.database.path)
    result = reopened.actions.execute(agent_id, "interrupted", "create_file", args)
    assert result["status"] == "unknown"
    assert not (folder / "file.txt").exists()


def test_read_tool_returns_real_file_content_and_execution_source(app, agent_id, tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "file.txt").write_text("真实内容", encoding="utf-8")
    app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
    result = app.actions.execute(agent_id, "read", "read_file", {"path": str(folder / "file.txt")})
    assert result["result"]["text"] == "真实内容"
    assert app.history.read(result["source"])["kind"] == "tool"


def test_unknown_tool_does_not_invoke_arbitrary_program(app, agent_id):
    with pytest.raises(ValueError):
        app.actions.execute(agent_id, "bad", "shell", {"command": "anything"})
