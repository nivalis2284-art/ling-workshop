"""M2 P0 measurement contract: one attachment count, sent bytes, checkpoint bound.

Three properties are locked here, all offline (temp DB plus MockTransport):

* an attachment's estimate is charged exactly once — the context builder counts
  it in ``extra_tokens``, so the payload manifest must not add it again;
* the bytes actually sent are not the intermediate candidate: the gateway adds
  the model name, the streaming flags and the native tool declarations;
* a checkpoint over the 8 MiB bound is refused before any write, so the private
  state and the side-effect ledger keep their previous contents.
"""

import asyncio
import json

import httpx
import pytest

from lingnexus.common.errors import Conflict
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import attachments
from lingnexus.runtime.checkpoints import RunCheckpoints
from lingnexus.runtime.payload import build_payload

CHECKPOINT_LIMIT = 8 * 1_048_576

# --- shared builders (shape matches tests/test_smoke.py) -------------------


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
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}
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
    from lingnexus.runtime import ChatRequest, ChatRuntime

    async def scenario():
        async with mock_client(responses, requests) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(**request))
            events = [event async for event in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            await runtime.aclose()
            return result, events

    return asyncio.run(scenario())


def checkpoint_row(app, run_id):
    with app.database.transaction() as db:
        row = db.execute(
            "SELECT state,updated_at,assistant_message_id FROM chat_run_checkpoints WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


# --- attachments are charged once, not once by the builder and again --------


def test_attachment_estimate_is_charged_once_not_twice(app, agent_id):
    configure(app)
    marker = "M2预算哨兵。"
    item = app.attachments.add(agent_id, "预算.txt", (marker * 200).encode())
    plan = attachments.describe(app, agent_id, item["id"])
    assert plan.text.count(marker) == 200
    conversation = app.conversations.create(agent_id, "预算对照")
    user = app.conversations.append_user(conversation["id"], "看附件", request_id="budget-user")
    messages = app.conversations.messages(conversation["id"])

    plain_payload, plain = build_payload(app, agent_id, profile(), messages)
    attachments.link(app, conversation["id"], user["id"], [plan])
    linked_payload, linked = build_payload(app, agent_id, profile(), messages)

    assert plain["attachments"] == []
    assert json.dumps(plain_payload, ensure_ascii=False).count(marker) == 0
    # The linked request grows by exactly one copy of the attachment's estimate.
    delta = linked["estimated_input_tokens"] - plain["estimated_input_tokens"]
    assert delta == plan.estimated_tokens
    assert delta != 2 * plan.estimated_tokens
    assert [entry["estimated_input_tokens"] for entry in linked["attachments"]] == [
        plan.estimated_tokens
    ]
    assert linked["attachments"][0]["id"] == item["id"]
    assert linked["attachments"][0]["message_id"] == user["id"]
    # The attachment body itself is sent once, and the request still is not
    # declared exact while a byte-based attachment estimate is part of it.
    assert json.dumps(linked_payload, ensure_ascii=False).count(marker) == 200
    assert linked["token_count_exact"] is False


# --- what is actually sent is not the intermediate candidate ----------------


def test_final_request_adds_the_fields_the_candidate_never_had(app, agent_id):
    from lingnexus.actions.dispatch import ToolDispatcher

    configure(app, context_window_tokens=20000, max_output_tokens=1000)
    conversation = app.conversations.create(agent_id, "请求对照")
    requests = []
    result, events = run_chat(
        app,
        [reply("收到")],
        {
            "agent_id": agent_id,
            "message": "对照问题",
            "request_id": "p0-final-body",
            "conversation_id": conversation["id"],
        },
        requests,
    )
    assert result["status"] == "completed"
    assert any(event["event"] == "complete" for event in events)
    assert len(requests) == 1
    body = requests[0]

    sent = app.conversations.messages(conversation["id"], head_id=result["user_message_id"])
    candidate, manifest = build_payload(
        app, agent_id, profile(context_window_tokens=20000, max_output_tokens=1000), sent
    )
    # Same stored history: the difference is only what the gateway adds.
    assert body["messages"] == candidate["messages"]
    assert set(candidate) - set(body) == set()
    assert {"tools", "model", "stream", "stream_options"} <= set(body) - set(candidate)
    assert "model" not in candidate and "stream" not in candidate and "tools" not in candidate
    assert body["model"] == "fixture"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_completion_tokens"] == 1000
    declared = [item["function"]["name"] for item in body["tools"]]
    assert declared == [spec["name"] for spec in ToolDispatcher(mcp=app.mcp).schema()]
    assert {"read_file", "run_command"} <= set(declared)
    # The persisted estimate describes the same candidate request the turn built.
    assert result["context_manifest"] == manifest
    # Candidate estimate + output reserve + real declarations still fit the window.
    assert (
        manifest["estimated_input_tokens"]
        + 1000
        + len(json.dumps(body["tools"], ensure_ascii=False).encode())
        <= 20000
    )


# --- the 8 MiB checkpoint bound refuses before it can overwrite anything -----


def test_oversized_checkpoint_is_rejected_without_overwriting_state_or_effects(app, agent_id):
    configure(app)
    result, _ = run_chat(
        app,
        [reply("完成")],
        {"agent_id": agent_id, "message": "检查点上限", "request_id": "p0-checkpoint-bound"},
    )
    assert result["status"] == "completed"
    run_id = result["id"]
    checkpoints = RunCheckpoints(app.database)
    original = checkpoints.load(run_id)
    assert original is not None and original["phase"] == "completed"

    def encoded(state):
        return json.dumps(state, ensure_ascii=False).encode()

    # Exactly at the bound is still accepted; one byte more is refused.
    at_limit = {**original, "padding": ""}
    at_limit["padding"] = "x" * (CHECKPOINT_LIMIT - len(encoded(at_limit)))
    assert len(encoded(at_limit)) == CHECKPOINT_LIMIT
    checkpoints.save(run_id, at_limit)
    checkpoints.begin(run_id, 1, "guard-call")
    checkpoints.finish(run_id, 1, "guard-call", ["sensitive_result_discarded", "unknown", False])
    before = checkpoint_row(app, run_id)
    assert before["state"] == encoded(at_limit).decode()

    oversized = {**at_limit, "padding": at_limit["padding"] + "x"}
    assert len(encoded(oversized)) == CHECKPOINT_LIMIT + 1
    with pytest.raises(Conflict, match="大小上限"):
        checkpoints.save(run_id, oversized)

    # No write happened: same row bytes, same effect ledger, same fence state.
    assert checkpoint_row(app, run_id) == before
    assert checkpoints.effect(run_id, 1, "guard-call") == {
        "status": "completed",
        "result": ["sensitive_result_discarded", "unknown", False],
    }
    assert checkpoints.uncertain(run_id) is False
    with app.database.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_run_checkpoints WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM chat_tool_effects WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 1
    # The writer still works after the refusal.
    checkpoints.save(run_id, original)
    assert checkpoints.load(run_id) == original


def test_runtime_window_guard_counts_gateway_added_fields_before_http(app, agent_id):
    from lingnexus.actions.catalog import registry
    from lingnexus.runtime.payload import declare_tools

    item = ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="long-model-" + "x" * 12000, base_url="https://fixture.test",
        context_window_tokens=16000, max_output_tokens=1000)
    saved = app.profiles.save("large native wrapper", item, api_key="runtime-secret-only")
    app.profiles.select(saved["id"], None)
    candidate, _ = build_payload(app, agent_id, item, [{"id": "synthetic", "role": "user", "text": "检查真实请求预算"}])
    declare_tools(candidate, item, registry.schema())
    assert len(json.dumps(candidate, ensure_ascii=False).encode()) + 1000 < 16000
    requests = []
    result, events = run_chat(app, [], {"agent_id": agent_id, "message": "检查真实请求预算", "request_id": "native-wrapper-limit"}, requests)
    assert requests == []
    assert result["status"] == "failed" and result["recoverable"] is False
    assert next(e for e in events if e["event"] == "error")["data"]["code"] == "context_limit"
