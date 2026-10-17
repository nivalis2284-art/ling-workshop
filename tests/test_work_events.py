"""M2 work boundaries exercised through real runtime and final HTTP bodies."""

import asyncio
import hashlib
import json

import httpx
import pytest
from test_run_recovery import _chat_reply, _final_response, _native_response, _sse

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime
from lingnexus.runtime.payload import build_payload


def configure(app, family=APIFamily.OPENAI_CHAT_COMPLETIONS, *, window=200000):
    profile = ModelProfile(api_family=family, model="fixture", base_url="https://fixture.test",
                           context_window_tokens=window, max_output_tokens=1000)
    saved = app.profiles.save("events", profile, api_key="event-secret")
    app.profiles.select(saved["id"], None)
    return profile


def final(family, text="答复已保留"):
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        return _chat_reply(text)
    if family == APIFamily.OPENAI_RESPONSES:
        return _sse({"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg-final", "type": "message", "role": "assistant", "content": []}}, {"type": "response.output_text.delta", "item_id": "msg-final", "output_index": 0, "content_index": 0, "delta": text}, {"type": "response.completed", "response": {"id": "resp-final", "status": "completed", "output": [
            {"id": "msg-final", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}
        ]}})
    return _final_response(family).replace("done", text)


async def chat(app, runtime, aid, cid, message, request_id, **kwargs):
    run = runtime.submit(ChatRequest(agent_id=aid, conversation_id=cid, message=message,
                                     request_id=request_id, **kwargs))
    events = [e async for e in runtime.events(run["id"])]
    assert not [e for e in events if e["event"] == "error"], json.dumps([e for e in events if e["event"] in {"notice", "error"}], ensure_ascii=True)
    assert runtime.get_run(run["id"])["status"] == "completed"
    return runtime.get_run(run["id"])


def closed_event(app, aid, *, body="允许保留的原始答复", request_id=None):
    configure(app)
    cid = app.conversations.create(aid, "工作")['id']
    request_id = request_id or cid
    event = app.work_events.begin(cid, "分析资料", request_id)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=_chat_reply(body)))) as client:
            runtime = ChatRuntime(app, client=client)
            run = await chat(app, runtime, aid, cid, "用户工作要求", "work:" + request_id)
            await runtime.aclose()
            return run

    run = asyncio.run(scenario())
    return cid, app.work_events.close(event["id"], expected_revision=1), run


@pytest.mark.parametrize("family", list(APIFamily))
def test_fold_is_whole_native_exchange_before_attachment_and_budget(app, agent_id, tmp_path, family):
    configure(app, family)
    folder = tmp_path / "allowed"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
    target = folder / "source.txt"
    raw = "工具全文哨兵-" * 400
    target.write_text(raw, encoding="utf-8")
    attachment = app.attachments.add(agent_id, "context.txt", b"ATTACHMENT_SENTINEL_529")
    cid = app.conversations.create(agent_id, "同一聊天")['id']
    requests, queue = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=queue.pop(0))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            queue.append(final(family, "工作前闲聊：雨后看山"))
            await chat(app, runtime, agent_id, cid, "PRE_WORK_TOPIC", "before")
            event = app.work_events.begin(cid, "检查原始材料", "boundary")
            queue.extend([_native_response(family, str(target)), final(family, "工作答复哨兵")])
            run = await chat(app, runtime, agent_id, cid, "工作要求哨兵", "working", attachment_ids=[attachment["id"]])
            ended = app.work_events.close(event["id"], expected_revision=1)
            assert ended["coverage"] == "retained" and ended["folded"]
            raw_event = app.work_events.set_fold(event["id"], False, expected_revision=ended["revision"])
            queue.append(final(family))
            raw_after = await chat(app, runtime, agent_id, cid, "回到原来的闲聊", "raw-after")
            raw_body = requests[-1]
            app.work_events.set_fold(event["id"], True, expected_revision=raw_event["revision"])
            queue.append(final(family))
            after = await chat(app, runtime, agent_id, cid, "回到原来的闲聊", "after", regenerate_message_id=raw_after["user_message_id"])
            assert after["context_manifest"]["work_events"][0]["sent"]
            await runtime.aclose()
            return run, ended, raw_body

    run, event, raw_candidate = asyncio.run(scenario())
    before_count = len(requests)
    scope = {"agent_id": agent_id, "conversation_id": cid}
    directory = app.work_events.directory(event["id"], **scope)
    full_tool = next(s for s in directory["items"] if s["kind"] == "tool_result")
    page = app.work_events.read_record(event["id"], full_tool["id"], **scope, max_bytes=16384)
    assert raw in page["text"]
    assert hashlib.sha256(page["text"].encode()).hexdigest() == page["sha256"]
    assert len(requests) == before_count  # owner browsing cannot invoke a model
    encoded = json.dumps(requests[-1], ensure_ascii=False)
    for secret in ("工作要求哨兵", "工具全文哨兵", "工作答复哨兵", "ATTACHMENT_SENTINEL_529", str(target), "opaque-reasoning", "native-signature"):
        assert secret not in encoded
    assert "PRE_WORK_TOPIC" in encoded and "回到原来的闲聊" in encoded
    assert encoded.count("【工作经历 " + event["id"] + "】") == 1
    assert "event_read" in encoded and requests[-1]["model"] == "fixture"
    # The raw baseline contains the actual native call/result; projection is
    # measured against detail that really existed in the comparable request.
    baseline = json.dumps(raw_candidate, ensure_ascii=False)
    assert "工具全文哨兵" in baseline and "ATTACHMENT_SENTINEL_529" in baseline
    assert len(encoded.encode()) < len(baseline.encode())
    with app.database.transaction() as db:
        state = json.loads(db.execute("SELECT state FROM chat_run_checkpoints WHERE run_id=?", (run["id"],)).fetchone()[0])
    assert state["phase"] == "completed"
    assert "工具全文哨兵" in json.dumps(state, ensure_ascii=False)


def test_event_idempotence_revision_branch_and_immutable_sources(app, agent_id):
    cid, event, run = closed_event(app, agent_id, request_id="begin")
    assert app.work_events.begin(cid, "分析资料", "begin")["id"] == event["id"]
    assert app.work_events.close(event["id"], expected_revision=1)["revision"] == event["revision"]
    scope = {"agent_id": agent_id, "conversation_id": cid}
    page = app.work_events.directory(event["id"], **scope, limit=1)
    assert page["next_cursor"]
    changed = app.work_events.set_summary(event["id"], "所有者修订的概况", expected_revision=event["revision"])
    with pytest.raises(Conflict):
        app.work_events.directory(event["id"], **scope, cursor=page["next_cursor"])
    with pytest.raises(Conflict):
        app.work_events.set_fold(event["id"], False, expected_revision=event["revision"])
    assert changed["summary_kind"] == "owner"
    with app.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM work_event_summaries WHERE event_id=?", (event["id"],)).fetchone()[0] == 2
    original_head = app.conversations.read(cid)["head_id"]
    app.conversations.edit_user(cid, run["user_message_id"], "新分支", request_id="edit")
    assert app.work_events.list(cid) == []
    with pytest.raises(NotFound):
        app.work_events.directory(event["id"], **scope)
    app.conversations.select_head(cid, original_head)
    assert app.work_events.get(event["id"])["valid"]
    assert app.conversations.messages(cid)[-1]["text"] == "允许保留的原始答复"


def test_revoked_grants_block_summary_raw_and_cursor(app, agent_id, tmp_path):
    cid, event, _ = closed_event(app, agent_id, body="REVOKED_PRIVATE_BODY")
    scope = {"agent_id": agent_id, "conversation_id": cid}
    cursor = app.work_events.directory(event["id"], **scope, limit=1)["next_cursor"]
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    assert app.work_events.get(event["id"])["summary"] == ""
    with pytest.raises(PermissionDenied):
        app.work_events.directory(event["id"], **scope, cursor=cursor)
    # Even explicitly retaining a raw strategy cannot resurrect revoked input.
    event = app.work_events.set_fold(event["id"], False, expected_revision=event["revision"])
    candidate, manifest = build_payload(app, agent_id, configure(app), app.conversations.messages(cid))
    assert "REVOKED_PRIVATE_BODY" not in json.dumps(candidate)
    assert manifest["work_events"][0]["strategy"] == "unavailable"


def test_capture_gap_stays_raw_and_unknown_effect_prevents_close(app, agent_id):
    cid, event, run = closed_event(app, agent_id)
    # A separate historical run lacking the new prospective ledger must never
    # gain a dishonest "complete history" label just by selecting its start.
    with app.database.transaction() as db:
        db.execute("DELETE FROM work_events WHERE id=?", (event["id"],))
        db.execute("DELETE FROM work_event_records WHERE run_id=?", (run["id"],))
    new = app.work_events.begin(cid, "旧记录", "legacy", start_message_id=run["user_message_id"])
    from lingnexus.runtime.checkpoints import RunCheckpoints
    checkpoints = RunCheckpoints(app.database)
    checkpoints.begin(run["id"], 2, "uncertain-write")
    with pytest.raises(Conflict, match="副作用"):
        app.work_events.close(new["id"], expected_revision=1)
    checkpoints.finish(run["id"], 2, "uncertain-write", ["safe", "completed", False])
    ended = app.work_events.close(new["id"], expected_revision=1)
    assert ended["coverage"] == "capture_gap" and ended["folded"] is False
    with pytest.raises(Conflict):
        app.work_events.set_fold(new["id"], True, expected_revision=ended["revision"])


def test_fold_restores_pre_work_topic_that_raw_budget_really_dropped(app, agent_id):
    configure(app, window=23000)
    cid = app.conversations.create(agent_id, "预算内回到闲聊")['id']
    bodies, queue = [], []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=queue.pop(0))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            queue.append(_chat_reply("闲聊答复"))
            await chat(app, runtime, agent_id, cid, "TOPIC_RESTORED_721", "smalltalk")
            event = app.work_events.begin(cid, "五轮独立已结束工作", "five-turns")
            for n in range(5):
                queue.append(_chat_reply(f"WORK_DETAIL_{n}_" + "d" * 4000))
                await chat(app, runtime, agent_id, cid, f"工作步骤 {n}", f"step-{n}")
            ended = app.work_events.close(event["id"], expected_revision=1)
            raw = app.work_events.set_fold(event["id"], False, expected_revision=ended["revision"])
            queue.append(_chat_reply("raw answer"))
            raw_run = await chat(app, runtime, agent_id, cid, "继续刚才闲聊", "raw-control")
            baseline = json.dumps(bodies[-1], ensure_ascii=False)
            assert "TOPIC_RESTORED_721" not in baseline
            assert "WORK_DETAIL_4_" in baseline  # actual detail was sent
            app.work_events.set_fold(event["id"], True, expected_revision=raw["revision"])
            queue.append(_chat_reply("folded answer"))
            await chat(app, runtime, agent_id, cid, "继续刚才闲聊", "fold-control", regenerate_message_id=raw_run["user_message_id"])
            folded = json.dumps(bodies[-1], ensure_ascii=False)
            assert "TOPIC_RESTORED_721" in folded and "继续刚才闲聊" in folded
            assert "WORK_DETAIL_" not in folded
            assert folded.count("【工作经历 " + event["id"] + "】") == 1
            assert len(folded.encode()) < len(baseline.encode())
            await runtime.aclose()

    asyncio.run(scenario())


def test_missing_attachment_disables_summary_and_input_projection(app, agent_id):
    cid, event, run = closed_event(app, agent_id)
    # Recreate the event over the same safe run after attaching a real asset.
    item = app.attachments.add(agent_id, "original.txt", b"PRIVATE_ATTACHMENT_872")
    from lingnexus.runtime import attachments
    attachments.link(app, cid, run["user_message_id"], [attachments.describe(app, agent_id, item["id"])])
    with app.database.transaction() as db:
        db.execute("DELETE FROM work_events WHERE id=?", (event["id"],))
    event = app.work_events.begin(cid, "附件来源", "asset", start_message_id=run["user_message_id"])
    event = app.work_events.close(event["id"], expected_revision=1)
    assert event["valid"]
    with app.database.transaction() as db:
        name = db.execute("SELECT blob_name FROM attachments WHERE id=?", (item["id"],)).fetchone()[0]
    (app.attachments.directory / name).unlink()
    value = app.work_events.get(event["id"])
    assert not value["valid"] and value["reason"] == "sources_unavailable"
    assert value["summary"] == "" and not value["folded"]
    candidate, manifest = build_payload(app, agent_id, configure(app), app.conversations.messages(cid))
    assert "PRIVATE_ATTACHMENT_872" not in json.dumps(candidate)
    assert manifest["work_events"][0]["strategy"] == "unavailable"


def test_semantic_record_survives_native_checkpoint_cap_failure(app, agent_id, tmp_path, monkeypatch):
    from lingnexus.actions.registry import ToolResult

    configure(app, window=30_000_000)
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    cid = app.conversations.create(agent_id, "独立原文与恢复覆盖")['id']
    event = app.work_events.begin(cid, "检查大结果", "large")
    body = "大" * 3_000_000
    calls = []

    async def scenario():
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_native_response(APIFamily.OPENAI_CHAT_COMPLETIONS, str(tmp_path / "large.txt")))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            async def execute(name, arguments, scope):
                return ToolResult("completed", body, None, None)
            monkeypatch.setattr(runtime.dispatcher, "execute", execute)
            run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=cid, message="保留大结果", request_id="too-large-native"))
            events = [e async for e in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            assert result["status"] == "failed" and result["recoverable"] is False
            assert next(e for e in events if e["event"] == "error")["data"]["code"] == "checkpoint_limit"
            await runtime.aclose()
            return result

    run = asyncio.run(scenario())
    assert len(calls) == 1
    event = app.work_events.close(event["id"], expected_revision=1)
    assert event["coverage"] == "retained" and event["folded"]
    assert event["outcome"] == "failed" and event["replay_coverage"] == "unavailable"
    record = next(r for r in app.work_events.records.for_runs([run["id"]]) if r["kind"] == "tool_result")
    assert record["byte_count"] == 9_000_000
    assert record["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert app.work_events.records.read(record["id"], offset=8_999_988, max_bytes=12)["text"] == "大" * 4
    from lingnexus.runtime.checkpoints import RunCheckpoints
    assert RunCheckpoints(app.database).effect(run["id"], 1, "read-call")["status"] == "completed"


def test_explicit_task_link_pending_delivery_and_root_anchor(app, agent_id):
    from lingnexus.common.types import new_id
    from lingnexus.tasks.store import TaskStore

    configure(app)
    with app.database.transaction() as db:
        profile_id = db.execute("SELECT id FROM model_profiles LIMIT 1").fetchone()[0]
    store = TaskStore(app.database)
    tid = new_id()
    from lingnexus.work_events.service import scope_snapshot
    store.insert(tid, {"agent_id": agent_id, "objective": "独立任务的真实目标", "request_id": "task"}, "fingerprint", profile_id, None, 8, {"source_grants": scope_snapshot(app, agent_id)})
    with app.database.transaction() as db:
        db.execute("UPDATE tasks SET status='completed',verified=0,final='任务可读最终答复' WHERE id=?", (tid,))
    cid = app.conversations.create(agent_id, "显式任务返回")['id']
    assert app.work_events.list(cid) == []  # time/agent proximity does not link
    event = app.work_events.begin(cid, "查看任务结果", "task-event")
    linked = app.work_events.link_task(event["id"], tid, expected_revision=1)
    assert app.work_events.link_task(event["id"], tid, expected_revision=linked["revision"])["revision"] == linked["revision"]
    review = store.review(tid, [{"path": "result.txt", "operation": "create"}])
    with pytest.raises(Conflict, match="交付"):
        app.work_events.close(event["id"], expected_revision=linked["revision"])
    store.review_status(review, "applied", {"status": "applied"})
    event = app.work_events.close(event["id"], expected_revision=linked["revision"])
    assert event["message_ids"] == [] and event["outcome"] == "completed"
    assert "未全部核验" in event["summary"]  # runtime completed is not semantic success
    app.conversations.append_user(cid, "聊聊刚才的任务", request_id="return-to-chat")
    payload, manifest = build_payload(app, agent_id, configure(app), app.conversations.messages(cid))
    assert "查看任务结果" in json.dumps(payload, ensure_ascii=False)
    assert manifest["work_events"][0]["replaced_messages"] == 0
    with app.database.transaction() as db:
        db.execute("UPDATE tasks SET final='已改变的最终答复' WHERE id=?", (tid,))
    assert app.work_events.get(event["id"])["reason"] == "sources_unavailable"
