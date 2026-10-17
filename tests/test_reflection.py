import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest

from lingnexus.continuity import Continuity
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.reflection import ReflectionRunner, calendar, partition, sources

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def setup(app, aid, turns=2, text="资料"):
    if not getattr(app, "continuity", None):
        app.continuity = Continuity(app)
    cid = app.conversations.create(aid, "主干")["id"]
    app.continuity.main.bind(aid, cid, 0)
    ids = []
    for i in range(turns):
        u = app.conversations.append_user(cid, text + str(i), request_id=f"u{i}")
        a = app.conversations.append_assistant(
            cid, "回答" + str(i), request_id=f"a{i}", expected_head=u["id"]
        )
        with app.database.transaction() as db:
            db.execute(
                "UPDATE conversation_messages SET created_at=? WHERE id IN (?,?)",
                ((NOW - timedelta(days=4)).isoformat(), u["id"], a["id"]),
            )
        ids.append((u["id"], a["id"]))
    return cid, ids


def profile(app, family=APIFamily.OPENAI_CHAT_COMPLETIONS, window=200000):
    return app.profiles.save(
        "reflection fixture",
        ModelProfile(
            api_family=family,
            model="offline",
            base_url="https://fixture.test",
            context_window_tokens=window,
            max_output_tokens=2048,
        ),
        api_key="fixture-reflection-secret",
    )["id"]


def sse(value, family=APIFamily.OPENAI_CHAT_COMPLETIONS, usage=True):
    text = json.dumps(value, ensure_ascii=False)
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        frame = {
            "id": "r",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
        if usage:
            frame["usage"] = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
        return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"
    if family == APIFamily.OPENAI_RESPONSES:
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "m", "type": "message", "role": "assistant", "content": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": "m",
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
            {
                "type": "response.completed",
                "response": {"id": "r", "status": "completed", "output": []},
            },
        ]
    else:
        events = [
            {
                "type": "message_start",
                "message": {"id": "r", "type": "message", "role": "assistant", "content": []},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events)


def mock(requests, family=APIFamily.OPENAI_CHAT_COMPLETIONS, hook=None, usage=True):
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if hook:
            custom = hook(len(requests), body)
            if custom is not None:
                return custom
        data = json.loads(body.get("input") or body["messages"][-1]["content"])
        units = data["来源单位"]
        instruction = (
            body.get("instructions") or body.get("system") or body["messages"][0]["content"]
        )
        if "不写摘要" in instruction:
            value = {"events": [{"source_ids": [u["source_id"]]} for u in units]}
        else:
            value = {
                "title": "独立事件",
                "summary": "只概括本事件提供的来源。",
                "source_ids": [u["source_id"] for u in units],
            }
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=sse(value, family, usage)
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def enable(runner, aid, pid, **kwargs):
    return runner.configure(
        aid,
        expected_revision=runner.settings(aid)["revision"],
        enabled=True,
        profile_id=pid,
        threshold_bytes=1,
        **kwargs,
    )


def rows(app, table):
    with app.database.transaction() as db:
        return [dict(r) for r in db.execute("SELECT * FROM " + table)]


@pytest.mark.parametrize("family", list(APIFamily))
def test_gateway_multievent_candidate_and_owner_publish(app, agent_id, family):
    cid, _ids = setup(app, agent_id)
    pid = profile(app, family)
    requests = []

    async def scenario():
        async with mock(requests, family) as client:
            r = ReflectionRunner(app, client=client, now=lambda: NOW)
            enable(r, agent_id, pid)
            job = r.enqueue(agent_id, "manual")
            value = await r.tick()
            assert value["job"]["status"] == "candidate", value
            assert value["job"]["calls"] == 3
            assert len(value["job"]["candidate_events"]) == 2
            assert not rows(app, "continuity_experiences")
            published = r.publish_candidate(job["id"], expected_revision=value["job"]["revision"])
            assert published["status"] == "done"
            assert len(published["result_refs"]) == 2
            assert len(requests) == 3
            assert len(app.conversations.messages(cid)) == 4
            await r.aclose()

    asyncio.run(scenario())
    for body in requests:
        assert "tools" not in body
        assert body["stream"] is True
        assert body["model"] == "offline"
    for event in rows(app, "continuity_experiences"):
        assert calendar.as_utc(event["occurred_start"]) == NOW - timedelta(days=4)


def test_local_disabled_no_profile_and_low_gate_preserve_sources(app, agent_id):
    _cid, _ids = setup(app, agent_id)
    requests = []

    async def scenario():
        async with mock(requests) as client:
            r = ReflectionRunner(app, client=client, now=lambda: NOW)
            assert r.settings(agent_id)["enabled"] is False
            assert r.preview(agent_id)["model_calls"] == 0
            assert r.enqueue(agent_id, "disabled")["status"] == "skipped"
            r.configure(agent_id, expected_revision=0, enabled=True)
            assert r.enqueue(agent_id, "no-profile")["error_code"] == "no_profile"
            pid = profile(app)
            r.configure(
                agent_id, expected_revision=1, enabled=True, profile_id=pid, threshold_bytes=32768
            )
            j = r.enqueue(agent_id, "low")
            assert (await r.tick())["job"]["error_code"] == "below_gate"
            assert r.preview(agent_id)["source_units"] == 2
            assert not rows(app, "reflection_snapshots")
            assert requests == []
            assert not rows(app, "continuity_experiences")
            assert r.enqueue(agent_id, "low")["id"] == j["id"]
            await r.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", range(30))
def test_frozen_structural_matrix(case):
    units = [
        {
            "source_id": f"turn:{i}",
            "island": 0,
            "occurred_at": (NOW - timedelta(days=5 - i)).isoformat(),
            "occurred_end": (NOW - timedelta(days=5 - i)).isoformat(),
        }
        for i in range(4)
    ]
    events = [
        {"title": "事件", "summary": "只读来源概况", "source_ids": ["turn:0", "turn:1"]},
        {"title": "事件二", "summary": "另一事件", "source_ids": ["turn:2", "turn:3"]},
    ]
    variant = case % 10
    if variant == 1:
        events[0]["source_ids"] = ["turn:0", "turn:0"]
    if variant == 2:
        events[0]["source_ids"] = ["foreign"]
    if variant == 3:
        events[1]["source_ids"] = ["turn:3"]
    if variant == 4:
        events[0]["source_ids"] = ["turn:1", "turn:0"]
    if variant == 5:
        events[0]["summary"] = ""
    if variant == 6:
        units[1]["island"] = 1
    if variant == 7:
        events[0]["source_ids"] = ["turn:0", "turn:2"]
        events[1]["source_ids"] = ["turn:1", "turn:3"]
    if variant == 8:
        events[0]["title"] = "大" * 501
    if variant in {0, 9}:
        clean = partition.parse({"events": events}, units, max_events=6)
        ReflectionRunner._check_cover(clean, units)
        assert clean[0]["occurred_start"] == units[0]["occurred_at"]
    else:
        from lingnexus.reflection.runner import StopWork

        with pytest.raises((partition.PartitionError, StopWork)):
            clean = partition.parse({"events": events}, units, max_events=6)
            ReflectionRunner._check_cover(clean, units)


def test_dst_gap_fold_and_skipped_date():
    ny = calendar.zone("America/New_York")
    assert (
        calendar.occurrence(date(2026, 3, 8), time(2, 30), ny)["due_utc"]
        == "2026-03-08T07:00:00+00:00"
    )
    assert (
        calendar.occurrence(date(2026, 11, 1), time(1, 30), ny)["due_utc"]
        == "2026-11-01T05:30:00+00:00"
    )


def test_cross_page_age_equal_oversize_and_islands(app, agent_id):
    _cid, ids = setup(app, agent_id, turns=80, text="字" * 30)
    with app.database.transaction() as db:
        db.execute(
            "UPDATE conversation_messages SET created_at=? WHERE id=?",
            ((NOW - timedelta(days=3)).isoformat(), ids[0][1]),
        )
        db.execute("UPDATE conversation_messages SET text=? WHERE id=?", ("x" * 200000, ids[20][0]))
    result = sources.scan(app, agent_id, cutoff=NOW - timedelta(days=3), max_source_bytes=131072)
    assert len(result["units"]) == 79
    assert result["bytes"] > 3276
    assert len({u["island"] for u in result["units"]}) == 2
    assert any(d["reason"] == "oversize_unit" for d in result["deferred"])
    assert all(len(u["sources"]) == 2 for u in result["units"])
