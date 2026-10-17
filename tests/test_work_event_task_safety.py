"""New task-to-event sources may not launder permissions or model credentials."""

import asyncio
import json

import pytest
from test_work_events import configure

from lingnexus.actions.registry import ToolResult
from lingnexus.common.errors import Conflict
from lingnexus.common.types import new_id
from lingnexus.tasks.execution import ToolExecutor
from lingnexus.tasks.store import TaskStore
from lingnexus.work_events.service import scope_snapshot


def create_task(app, aid, *, scope=None):
    configure(app)
    pid = app.profiles.selection()["primary"]
    store = TaskStore(app.database)
    tid = new_id()
    store.insert(tid, {"agent_id": aid, "objective": "来源安全测试", "request_id": tid}, "fp", pid, None, 8, {} if scope is None else {"source_grants": scope})
    return store, store.get(tid)


def test_old_or_revoked_task_cannot_be_relabelled_as_new_chat_sources(app, agent_id, tmp_path):
    _, old = create_task(app, agent_id)
    cid = app.conversations.create(agent_id, "来源隔离")['id']
    event = app.work_events.begin(cid, "显式关联也不能扩权", "task-scope")
    with pytest.raises(Conflict, match="授权快照"):
        app.work_events.link_task(event["id"], old["id"], expected_revision=1)
    old_scope = scope_snapshot(app, agent_id)
    _, task = create_task(app, agent_id, scope=old_scope)
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    cid2 = app.conversations.create(agent_id, "授权已改变")['id']
    event2 = app.work_events.begin(cid2, "新事件", "task-new-scope")
    with pytest.raises(Conflict, match="授权快照"):
        app.work_events.link_task(event2["id"], task["id"], expected_revision=1)
    with app.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM work_event_task_links").fetchone()[0] == 0


def test_task_tool_key_echo_is_not_saved_in_new_readable_sources(app, agent_id, monkeypatch):
    store, task = create_task(app, agent_id, scope=scope_snapshot(app, agent_id))
    executor = ToolExecutor(app, store, None)

    async def execute(name, arguments, scope):
        return ToolResult("completed", [{"status": "completed", "text": "event-secret"}], None, None)

    monkeypatch.setattr(executor.dispatcher, "execute", execute)
    result = asyncio.run(executor.execute(task, {"id": "key-echo", "name": "read_file", "arguments": {"path": "fixture"}}))
    assert result == {"status": "unknown", "error_code": "credential_in_response"}
    with app.database.transaction() as db:
        steps = [dict(r) for r in db.execute("SELECT * FROM task_steps WHERE task_id=?", (task["id"],))]
        events = [dict(r) for r in db.execute("SELECT * FROM task_events WHERE task_id=?", (task["id"],))]
    assert steps[0]["status"] == "unknown"
    assert "event-secret" not in json.dumps([steps, events])
    assert "credential_in_response" in steps[0]["result"]
