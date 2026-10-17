"""Owner-maintained smoke layer: the first part's survival paths.

This file covers only the standalone Agent system — conversations, attachments,
model access, tasks, permissions, maintenance and the local API boundary. The
long-term part (memory, identity, history, learning, skills, development,
activities) is not assembled; its cases were retired and are recorded in
`test_smoke_part2_cases.retired.md`.

Keep this layer fast (tens of seconds), keep it honest, and keep every assertion
connected to a real failure mode.
"""

import asyncio
import json

import httpx
import pytest

from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.context.builder import BudgetPolicy
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime.payload import build_payload
from lingnexus.runtime.types import ChatRequest

# --- shared builders ------------------------------------------------------


def profile(*, context_window_tokens=None, max_output_tokens=None):
    return ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
    )


def configure(app, **limits):
    item = app.profiles.save("fixture", profile(**limits), api_key="runtime-secret-only")
    app.profiles.select(item["id"], None)
    return item["id"]


def reply(text):
    """One OpenAI chat-completions SSE turn carrying visible assistant text."""
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def tool_reply(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {
                "index": i,
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (cid, name, args) in enumerate(calls)
        ]
    frame = {
        "id": "chat-native",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def mock_client(responses, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        value = responses.pop(0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=value)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_chat(app, responses, request, requests=None):
    """Submit one chat turn against a mocked model; return (result, events)."""
    from lingnexus.runtime import ChatRuntime

    async def scenario():
        async with mock_client(responses, requests) as client:
            runtime = ChatRuntime(app, client=client)
            value = request if isinstance(request, ChatRequest) else ChatRequest(**request)
            run = runtime.submit(value)
            events = [event async for event in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            await runtime.aclose()
            return result, events

    return asyncio.run(scenario())


def _turn(app, conversation_id, text, request_id):
    user = app.conversations.append_user(conversation_id, text, request_id=request_id)
    answer = app.conversations.append_assistant(
        conversation_id, "回复" + text, request_id=request_id + "-a", expected_head=user["id"]
    )
    return user, answer


# --- conversations survive restart ---------------------------------------


def test_reopen_preserves_conversation_text(app_factory, tmp_path):
    first = app_factory(tmp_path / "ling.db")
    agent = first.create_agent("灵")
    conversation = first.conversations.create(agent, "日常交流")
    first.conversations.append_user(conversation["id"], "记住咖啡", request_id="u1")
    first.close()
    second = app_factory(tmp_path / "ling.db")
    assert [row["text"] for row in second.conversations.messages(conversation["id"])] == [
        "记住咖啡"
    ]
    second.close()


def test_duplicate_request_appends_one_message(app, agent_id):
    conversation = app.conversations.create(agent_id, "日常交流")
    first = app.conversations.append_user(conversation["id"], "内容", request_id="same")
    assert app.conversations.append_user(conversation["id"], "内容", request_id="same") == first
    assert len(app.conversations.messages(conversation["id"])) == 1


def test_duplicate_request_with_changed_content_is_rejected(app, agent_id):
    conversation = app.conversations.create(agent_id, "日常交流")
    app.conversations.append_user(conversation["id"], "原文", request_id="same")
    with pytest.raises(Conflict):
        app.conversations.append_user(conversation["id"], "变化", request_id="same")
    assert len(app.conversations.messages(conversation["id"])) == 1


def test_edit_user_forks_a_new_branch(app, agent_id):
    conversation = app.conversations.create(agent_id, "日常交流")
    user = app.conversations.append_user(conversation["id"], "喜欢咖啡", request_id="coffee")
    app.conversations.append_assistant(
        conversation["id"], "记住咖啡", request_id="answer", expected_head=user["id"]
    )
    edited = app.conversations.edit_user(
        conversation["id"], user["id"], "喜欢茶", request_id="tea"
    )
    assert edited["id"] != user["id"]
    assert edited["parent_id"] is None
    assert [row["text"] for row in app.conversations.messages(conversation["id"])] == ["喜欢茶"]
    tips = {branch["id"] for branch in app.conversations.branches(conversation["id"])}
    assert edited["id"] in tips


def test_branch_switch_moves_the_selected_path(app, agent_id):
    conversation = app.conversations.create(agent_id, "日常交流")
    user = app.conversations.append_user(conversation["id"], "原问题", request_id="u")
    answer = app.conversations.append_assistant(
        conversation["id"], "原回答", request_id="a", expected_head=user["id"]
    )
    edited = app.conversations.edit_user(
        conversation["id"], user["id"], "改后问题", request_id="u2"
    )
    app.conversations.append_assistant(
        conversation["id"], "改后回答", request_id="a2", expected_head=edited["id"]
    )
    assert [row["text"] for row in app.conversations.messages(conversation["id"])] == [
        "改后问题",
        "改后回答",
    ]
    app.conversations.select_head(conversation["id"], answer["id"])
    assert app.conversations.read(conversation["id"])["head_id"] == answer["id"]
    assert [row["text"] for row in app.conversations.messages(conversation["id"])] == [
        "原问题",
        "原回答",
    ]


def test_delete_turn_removes_the_whole_round(app, agent_id):
    conversation = app.conversations.create(agent_id, "日常交流")
    _turn(app, conversation["id"], "你好", "u1")
    second, _ = _turn(app, conversation["id"], "喜欢咖啡吗", "u2")
    app.conversations.delete_turns(conversation["id"], [second["id"]])
    assert [row["text"] for row in app.conversations.messages(conversation["id"])] == [
        "你好",
        "回复你好",
    ]


# --- context budget: truncate and report, never fail ----------------------


def test_long_history_is_truncated_and_reported(app, agent_id):
    entries = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "text": "旧交流" + str(i) + ":" + "重复过程" * 90,
        }
        for i in range(20)
    ]
    newest = {"role": "user", "text": "继续当前问题"}
    packet = app.context.build(
        agent_id,
        messages=[*entries, newest],
        budget=BudgetPolicy(context_limit=8000, output_reserve=1000, tool_reserve=256),
    )
    assert packet.evidence[-1]["text"] == "继续当前问题"
    assert packet.manifest["truncated_messages"] > 0
    assert packet.manifest["sent_messages"] < 21


def test_payload_keeps_the_newest_tail_under_budget(app, agent_id):
    messages = [
        {
            "id": str(i),
            "role": "user" if i % 2 == 0 else "assistant",
            "text": "过程" * 200,
        }
        for i in range(20)
    ]
    messages.append({"id": "new", "role": "user", "text": "继续当前问题"})
    payload, manifest = build_payload(
        app, agent_id, profile(context_window_tokens=8000, max_output_tokens=1000), messages
    )
    assert payload["messages"][-1]["content"] == "继续当前问题"
    assert manifest["truncated_messages"] > 0


def test_over_budget_turn_truncates_with_a_notice(app, agent_id):
    # The window must fit the real tool declarations as well as a recent tail.
    # Smaller-than-schema windows now stop before issuing a provider request.
    configure(app, context_window_tokens=20000, max_output_tokens=1000)
    conversation = app.conversations.create(agent_id, "长对话")
    head = None
    for i in range(20):
        text = "旧交流" + str(i) + ":" + "重复过程" * 90
        if i % 2 == 0:
            node = app.conversations.append_user(conversation["id"], text, request_id=f"h{i}")
        else:
            node = app.conversations.append_assistant(
                conversation["id"], text, request_id=f"h{i}", expected_head=head
            )
        head = node["id"]
    result, events = run_chat(
        app,
        [reply("继续回答")],
        {
            "agent_id": agent_id,
            "message": "继续当前问题",
            "request_id": "over-budget",
            "conversation_id": conversation["id"],
        },
    )
    assert result["status"] == "completed"
    assert result["context_manifest"]["truncated_messages"] > 0
    notices = [event for event in events if event["event"] == "notice"]
    assert any(item["data"]["code"] == "context_truncated" for item in notices)
    assert any(event["event"] == "complete" for event in events)


# --- attachments ----------------------------------------------------------


def test_attachment_is_uploaded_and_sent_with_the_turn(app, agent_id):
    configure(app)
    item = app.attachments.add(agent_id, "notes.txt", "会议记录：预算1760。".encode())
    requests = []
    result, events = run_chat(
        app,
        [reply("收到附件")],
        {
            "agent_id": agent_id,
            "message": "看附件",
            "request_id": "att-1",
            "attachment_ids": [item["id"]],
        },
        requests,
    )
    assert result["status"] == "completed"
    assert any(event["event"] == "complete" for event in events)
    assert "预算1760" in json.dumps(requests[0], ensure_ascii=False)
    user = next(
        row
        for row in app.conversations.messages(result["conversation_id"])
        if row["role"] == "user"
    )
    assert user["attachment_ids"] == [item["id"]]
    assert result["context_manifest"]["attachments"][0]["id"] == item["id"]


def test_attachment_from_another_agent_is_rejected(app, agent_id):
    configure(app)
    other = app.create_agent("别")
    item = app.attachments.add(other, "secret.txt", b"private")
    with pytest.raises(PermissionDenied):
        run_chat(
            app,
            [reply("不应到达")],
            {
                "agent_id": agent_id,
                "message": "偷看",
                "request_id": "att-foreign",
                "attachment_ids": [item["id"]],
            },
        )


# --- task runtime ---------------------------------------------------------


async def settle(runtime, tid):
    for _ in range(1500):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            return runtime.get(tid)
        await asyncio.sleep(0.01)
    pytest.fail("task worker did not reach a checkpoint")


def test_task_writes_verifies_and_completes_with_private_state(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TASK_TABLES, TaskRuntime

        configure(app)
        folder = tmp_path / "authorized"
        folder.mkdir()
        target = folder / "answer.txt"
        app.permissions.configure(
            agent_id, readable=[folder], writable=[folder], expected_revision=0
        )
        requests = []
        replies = [
            tool_reply(
                "Plan: write then check.",
                [("write-id", "create_file", {"path": str(target), "text": "actual artifact"})],
            ),
            tool_reply(calls=[("check-id", "read_file", {"path": str(target)})]),
            tool_reply("Delivered and checked answer.txt."),
        ]
        async with mock_client(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            spec = {
                "agent_id": agent_id,
                "objective": "Create and verify answer.txt",
                "request_id": "durable-one",
            }
            tid = await runtime.submit(spec)
            with pytest.raises(Conflict):
                await runtime.submit({**spec, "objective": "changed"})
            result = await settle(runtime, tid)
            assert result["status"] == "completed", result
            assert result["verified"] is True
            assert target.read_text() == "actual artifact"
            assert requests[1]["messages"][-1]["tool_call_id"] == "write-id"
            assert requests[2]["messages"][-1]["tool_call_id"] == "check-id"
            events = runtime.events(tid)
            assert {"plan", "tool_started", "tool_result", "final", "status"} <= {
                event["type"] for event in events
            }
            with app.database.transaction() as db:
                for table in TASK_TABLES:
                    rows = db.execute("SELECT * FROM " + table).fetchall()
                    assert "runtime-secret-only" not in repr([tuple(r) for r in rows])
            await runtime.close()

    asyncio.run(run())


def test_task_rejects_unregistered_agent(app, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime

        configure(app)
        async with mock_client([tool_reply("unused")]) as client:
            runtime = TaskRuntime(app, client)
            with pytest.raises(PermissionDenied):
                await runtime.submit(
                    {"agent_id": "missing", "objective": "work", "request_id": "t"}
                )
            await runtime.close()

    asyncio.run(run())


# --- api boundary and security -------------------------------------------


def test_local_api_denies_missing_token_and_foreign_origin(client, headers):
    assert client.get("/health").status_code == 200
    assert client.get("/agents").status_code == 401
    assert (
        client.get(
            "/agents", headers=headers | {"Origin": "https://attacker.example"}
        ).status_code
        == 403
    )
    assert (
        client.get("/agents", headers=headers | {"Origin": "http://localhost:8765"}).status_code
        == 200
    )


def test_agent_registration_is_minimal(client, headers):
    created = client.post("/agents", headers=headers, json={"name": "灵"})
    assert created.status_code == 201
    listing = client.get("/agents", headers=headers).json()
    assert [item["name"] for item in listing] == ["灵"]


def test_reflection_service_is_mounted_in_the_real_lifespan(client, headers):
    created = client.post("/agents", headers=headers, json={"name": "反思起点"})
    assert created.status_code == 201
    agent_id = created.json()["id"]
    response = client.get(f"/agents/{agent_id}/reflection-settings", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False


def test_blank_agent_name_is_rejected(client, headers):
    assert client.post("/agents", headers=headers, json={"name": "   "}).status_code == 422


def test_compaction_settings_are_available_through_the_real_api(client, headers):
    created = client.post("/agents", headers=headers, json={"name": "压缩起点"})
    assert created.status_code == 201
    agent_id = created.json()["id"]
    current = client.get(f"/agents/{agent_id}/compaction-settings", headers=headers)
    assert current.status_code == 200, current.text
    assert current.json()["enabled"] is False
    changed = client.put(
        f"/agents/{agent_id}/compaction-settings",
        headers=headers,
        json={
            "expected_revision": 0,
            "enabled": True,
            "profile_modes": {},
            "trigger_ratio": 0.8,
            "target_ratio": 0.5,
            "max_summary_bytes": 16384,
            "max_epochs": 16,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["enabled"] is True
    assert changed.json()["revision"] == 1
