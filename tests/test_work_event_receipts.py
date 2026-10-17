"""Two real recall rounds must not permanently inflate later native requests."""

import asyncio
import json

import httpx
from test_run_recovery import _chat_reply, _sse
from test_work_events import chat, configure

from lingnexus.runtime import ChatRuntime


def call_frame(name, args, call_id):
    return _sse({"id": "recall-native", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
        {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    ]}, "finish_reason": "tool_calls"}]})


def test_two_events_two_recall_rounds_receipts_and_revocation(app, agent_id, tmp_path):
    configure(app)
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[tmp_path], expected_revision=0)
    cid = app.conversations.create(agent_id, "持续闲聊与两次工作")['id']
    app.access.set(cid, "full")
    queue, requests, events, sources, read_runs = [], [], [], [], []
    originals = ["FIRST_SOURCE_DETAIL_" + "甲" * 3000, "SECOND_SOURCE_DETAIL_" + "乙" * 3000]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=queue.pop(0))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            queue.append(_chat_reply("工作前话题保留"))
            await chat(app, runtime, agent_id, cid, "CONTINUOUS_TOPIC", "before-two-events")
            for i in range(2):
                event = app.work_events.begin(cid, f"第{i+1}件工作", f"event-{i}")
                queue.append(_chat_reply(originals[i]))
                await chat(app, runtime, agent_id, cid, f"执行第{i+1}件工作", f"work-{i}")
                event = app.work_events.close(event["id"], expected_revision=1)
                events.append(event)
                directory = app.work_events.directory(event["id"], agent_id=agent_id, conversation_id=cid)
                source = next(s for s in directory["items"] if s["kind"] == "message" and s["byte_count"] > 8000)
                sources.append(source)
                queue.append(_chat_reply("继续闲聊"))
                await chat(app, runtime, agent_id, cid, f"第{i+1}次工作后的闲聊", f"smalltalk-{i}")
            for i, event in enumerate(events):
                queue.extend([
                    call_frame("event_records", {"event_id": event["id"]}, f"directory-{i}"),
                    call_frame("event_read", {"event_id": event["id"], "source_id": sources[i]["id"], "max_bytes": 4096}, f"page-{i}"),
                    _chat_reply(f"LONG_RECALL_ANSWER_{i}_" + "可见原答复" * 300),
                ])
                before = len(requests)
                run = await chat(app, runtime, agent_id, cid, f"回查第{i+1}件工作的具体证据", f"recall-{i}")
                read_runs.append(run)
                assert len(requests) == before + 3
                body = json.dumps(requests[-1], ensure_ascii=False)
                assert originals[i][:40] in body  # exact source page entered THIS run
                assert sources[i]["sha256"] in body and "event_records" in body
                if i:
                    first_request = json.dumps(requests[before], ensure_ascii=False)
                    assert "FIRST_SOURCE_DETAIL_" not in first_request
                    assert "LONG_RECALL_ANSWER_0_" not in first_request
            queue.append(_chat_reply("闲聊继续"))
            after = await chat(app, runtime, agent_id, cid, "再回到最初的话题", "after-two-reads")
            body = json.dumps(requests[-1], ensure_ascii=False)
            for marker in ("FIRST_SOURCE_DETAIL_", "SECOND_SOURCE_DETAIL_", "LONG_RECALL_ANSWER_0_", "LONG_RECALL_ANSWER_1_"):
                assert marker not in body
            assert "CONTINUOUS_TOPIC" in body
            for event in events:
                assert body.count("【工作经历 " + event["id"] + "】") == 1
            receipts = [p for p in after["context_manifest"]["work_events"] if p["strategy"] == "read_receipt"]
            assert len(receipts) == 2 and len({p["projection_id"] for p in receipts}) == 2
            stored = app.conversations.messages(cid)
            assert sum("LONG_RECALL_ANSWER_" in m["text"] for m in stored) == 2

            # A read mixed with an actual write is deliberately ineligible for
            # the receipt shortcut. It retains its complete native exchange.
            target = tmp_path / "mixed.txt"
            queue.extend([
                call_frame("event_read", {"event_id": events[0]["id"], "source_id": sources[0]["id"], "max_bytes": 2048}, "mixed-read"),
                call_frame("create_file", {"path": str(target), "text": "side effect retained"}, "mixed-write"),
                _chat_reply("混合回合完成"), _chat_reply("后续答复"),
            ])
            mixed = await chat(app, runtime, agent_id, cid, "回查并创建一个文件", "mixed")
            assert target.read_text() == "side effect retained"
            following = await chat(app, runtime, agent_id, cid, "混合回合之后", "after-mixed")
            assert "FIRST_SOURCE_DETAIL_" in json.dumps(requests[-1], ensure_ascii=False)
            assert mixed["id"] not in {p.get("run_id") for p in following["context_manifest"]["work_events"] if p["strategy"] == "read_receipt"}

            # Withdrawal invalidates both original event and later derived
            # read/native caches, including a mixed turn containing a tool.
            app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=1)
            queue.append(_chat_reply("授权变化后的新答复"))
            await chat(app, runtime, agent_id, cid, "撤回后继续", "after-withdrawal")
            body = json.dumps(requests[-1], ensure_ascii=False)
            assert "FIRST_SOURCE_DETAIL_" not in body and "SECOND_SOURCE_DETAIL_" not in body
            assert "LONG_RECALL_ANSWER_" not in body
            await runtime.aclose()

    asyncio.run(scenario())
