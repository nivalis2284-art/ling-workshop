"""Independent frozen negative acceptance; only synthetic temporary data.

Missing modules are pending at collection/runtime. An available interface which
accepts an invalid batch is a failure, never xfail/skip or a relaxed assertion.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fixtures.m2_v2.protocols import profile, response_for
from fixtures.m2_v2.scenarios import SCENARIOS, SYSTEM_PROBES, partition_case

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied
from lingnexus.models import APIFamily


@pytest.fixture
def continuity(app):
    if importlib.util.find_spec("lingnexus.continuity") is None:
        pytest.skip("pending A: continuity module has not landed")
    from lingnexus.continuity import Continuity

    if not getattr(app, "continuity", None):
        app.continuity = Continuity(app)
    return app.continuity


def test_frozen_catalog_covers_requirements_without_claiming_live_quality():
    assert len(SCENARIOS) >= 30
    assert len({case.id for case in SCENARIOS}) == len(SCENARIOS)
    assert set(SYSTEM_PROBES) == {f"N{i}" for i in range(1, 18)}
    assert "UNVERIFIED" in SYSTEM_PROBES["N17"]


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: case.id)
def test_frozen_partition_cases_against_product_validator(case):
    package = importlib.util.find_spec("lingnexus.reflection")
    if package is None:
        pytest.skip("pending C: reflection package has not landed")
    # __init__ imports runner while the owner is still writing that module.
    # Do not confuse this known incomplete landing with a validator failure.
    from pathlib import Path
    if not (Path(package.origin).parent / "runner.py").exists():
        pytest.skip("pending C: package entry imports runner.py, still being implemented")
    from lingnexus.reflection.partition import PartitionError, parse
    from lingnexus.reflection.runner import ReflectionRunner, StopWork

    units, proposal = partition_case(case)
    if case.reject:
        with pytest.raises((PartitionError, StopWork)):
            parsed = parse(proposal, units, max_events=6)
            ReflectionRunner._check_cover(parsed, units)
        return
    result = parse(proposal, units, max_events=6)
    ReflectionRunner._check_cover(result, units)
    references = [ref for event in result for ref in event["source_ids"]]
    assert references == [unit["source_id"] for unit in units]
    by_id = {unit["source_id"]: unit for unit in units}
    for event in result:
        members = [by_id[ref] for ref in event["source_ids"]]
        assert datetime.fromisoformat(event["occurred_start"]) == min(
            datetime.fromisoformat(unit["occurred_at"]) for unit in members
        )
        # This parser only derives the instant-based partition metadata. The
        # runner enriches end-of-turn ranges; the real HTTP runner test below
        # checks that end-to-end requirement rather than prescribing which
        # internal helper must implement it.
        assert datetime.fromisoformat(event["occurred_end"]) in {
            datetime.fromisoformat(unit[key]) for unit in members
            for key in ("occurred_at", "occurred_end")
        }


def _seed(app, continuity, aid):
    cid = app.conversations.create(aid, "合成主干")["id"]
    continuity.main.bind(aid, cid, 0)
    units = []
    for i in range(3):
        user = app.conversations.append_user(cid, f"来源原文-{i}-🙂", request_id=f"seed-{i}")
        answer = app.conversations.append_assistant(cid, "未核验，下一步待办", request_id=f"answer-{i}",
                                                    expected_head=user["id"])
        from lingnexus.work_events.service import scope_snapshot
        for message in (user, answer):
            text = message["text"]
            units.append({"source_id": f"message:{message['id']}", "kind": "message",
                          "ref": {"id": message["id"]}, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                          "byte_count": len(text.encode()), "occurred_at": message["created_at"],
                          "revision": 1, "atomic_unit_id": f"turn:{user['id']}", "island": 0,
                          "grants": scope_snapshot(app, aid)})
    return cid, units


def _events(units):
    return [{"title": "合成事件", "summary": "仅讨论，未核验且有待办。",
             "occurred_start": units[0]["occurred_at"],
             "occurred_end": units[-1]["occurred_at"],
             "sources": [{"source_id": u["source_id"]} for u in units]}]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign"])
def test_publication_rejects_bad_cover_without_partial_rows(app, agent_id, continuity, mutation):
    cid, units = _seed(app, continuity, agent_id)
    draft = continuity.stage_reflection_batch(agent_id, conversation_id=cid, units=units,
                    batch_key="bad-cover", policy_version="reflection-v1")
    events = _events(units)
    if mutation == "missing": events[0]["sources"].pop()
    elif mutation == "duplicate": events[0]["sources"].append(events[0]["sources"][0].copy())
    else: events[0]["sources"].append({"source_id": "foreign"})
    with pytest.raises(Conflict):
        continuity.publish_reflection_batch(draft["id"], expected_revision=1, events=events)
    with app.database.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM continuity_experiences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM continuity_experience_sources").fetchone()[0] == 0
        assert db.execute("SELECT status FROM continuity_reflection_drafts WHERE id=?", (draft["id"],)).fetchone()[0] == "staged"


@pytest.mark.parametrize("mutation", ["edit", "delete", "revoke", "rebind"])
def test_frozen_candidate_cannot_publish_after_source_invalidation(
    app, agent_id, continuity, tmp_path, mutation
):
    cid, units = _seed(app, continuity, agent_id)
    draft = continuity.stage_reflection_batch(agent_id, conversation_id=cid, units=units,
                    batch_key="stale-candidate", policy_version="reflection-v1")
    if mutation == "edit":
        with app.database.transaction() as db:
            db.execute("UPDATE conversation_messages SET text='changed' WHERE id=?", (units[0]["ref"]["id"],))
    elif mutation == "delete":
        app.conversations.delete_turns(cid, [units[0]["ref"]["id"]])
    elif mutation == "revoke":
        folder = tmp_path / "grant"
        folder.mkdir()
        app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
    else:
        continuity.main.bind(agent_id, None, 1)
    with pytest.raises((Conflict, PermissionDenied, NotFound)):
        continuity.publish_reflection_batch(draft["id"], expected_revision=1, events=_events(units))
    with app.database.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM continuity_experiences").fetchone()[0] == 0


def test_main_binding_cas_preserves_legacy_messages(app, agent_id, continuity, app_factory):
    cid, units = _seed(app, continuity, agent_id)
    before = app.conversations.messages(cid)
    other = app.conversations.create(agent_id, "历史资料")
    with pytest.raises(Conflict):
        continuity.main.bind(agent_id, other["id"], 0)
    assert app.conversations.messages(cid) == before
    restarted = app_factory(app.database.path)
    try:
        from lingnexus.continuity import Continuity
        restarted.continuity = Continuity(restarted)
        assert restarted.continuity.main(agent_id)["conversation"]["id"] == cid
        assert restarted.conversations.messages(cid) == before
        assert {u["ref"]["id"] for u in units}.issubset({m["id"] for m in before})
    finally:
        restarted.close()


def _age_sources(app, cid):
    messages = app.conversations.messages(cid)
    with app.database.transaction() as db:
        for i, message in enumerate(messages):
            stamp = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i)
            db.execute("UPDATE conversation_messages SET created_at=? WHERE id=?",
                       (stamp.isoformat(), message["id"]))
    return app.conversations.messages(cid)


@pytest.mark.parametrize("family", list(APIFamily))
def test_low_gate_zero_http_zero_cover_and_next_scan_accumulates(app, agent_id, continuity, family):
    from lingnexus.reflection import ReflectionRunner

    cid, _ = _seed(app, continuity, agent_id)
    _age_sources(app, cid)
    pid = app.profiles.save("reflection-fixture", profile(family), api_key="synthetic-reflection-key")["id"]
    requests = []

    def forbidden_http(request):
        requests.append(request)
        raise AssertionError("below gate must make ZERO model HTTP requests")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_http)) as client:
            runner = ReflectionRunner(app, client=client, now=lambda: datetime(2026, 10, 17, tzinfo=UTC))
            try:
                runner.configure(agent_id, expected_revision=0, enabled=True, profile_id=pid,
                                 min_age_days=3, threshold_bytes=32768)
                initial = runner.preview(agent_id)
                job = runner.enqueue(agent_id, "below-gate-1")
                await runner.tick()
                value = runner.get(job["id"])
                assert value["status"] == "skipped", value
                assert value["calls"] == 0
                assert requests == []
                with app.database.transaction() as db:
                    for table in ("continuity_experiences", "continuity_experience_sources",
                                  "reflection_snapshots", "reflection_calls"):
                        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                first = app.conversations.append_user(cid, "累积候选" * 40, request_id="accumulate")
                app.conversations.append_assistant(cid, "未完成", request_id="accumulate-answer", expected_head=first["id"])
                _age_sources(app, cid)
                accumulated = runner.preview(agent_id)
                assert accumulated["candidate_bytes"] > initial["candidate_bytes"] > 0
                assert accumulated["source_units"] == initial["source_units"] + 1
                assert requests == [], "preview and skipped history must remain free"
            finally:
                await runner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_reflection_real_http_candidate_trusted_times_and_stale_publication(
    app, agent_id, continuity, family
):
    from lingnexus.reflection import ReflectionRunner

    cid, _ = _seed(app, continuity, agent_id)
    original = _age_sources(app, cid)
    pid = app.profiles.save("reflect", profile(family, context_window_tokens=200_000),
                            api_key="synthetic-reflection-key")["id"]
    bodies, ids = [], []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        native = body.get("input", body.get("messages"))
        content = native if isinstance(native, str) else native[-1]["content"]
        payload = json.loads(content)
        selected = [u["source_id"] for u in payload["来源单位"]]
        ids.extend(selected)
        output = ({"events": [{"source_ids": selected}]} if len(bodies) == 1 else
                  {"title": "讨论和待办", "summary": "尚未核验，仍需继续。", "source_ids": selected,
                   "occurred_start": "2099-01-01T00:00:00Z", "occurred_end": "2099-01-02T00:00:00Z"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=response_for(family, json.dumps(output, ensure_ascii=False)))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = ReflectionRunner(app, client=client, now=lambda: datetime(2026, 10, 17, tzinfo=UTC))
            try:
                runner.configure(agent_id, expected_revision=0, enabled=True, profile_id=pid, threshold_bytes=1)
                job = runner.enqueue(agent_id, "candidate-http")
                await runner.tick()
                value = runner.get(job["id"])
                assert value["status"] == "candidate", value
                assert value["calls"] == 2
                assert len(bodies) == 2
                assert all(not b.get("tools") for b in bodies), "reflection worker is readonly"
                assert all(m["text"] in json.dumps(bodies[0], ensure_ascii=False) for m in original)
                with app.database.transaction() as db:
                    assert db.execute("SELECT COUNT(*) FROM continuity_experiences").fetchone()[0] == 0
                published = runner.publish_candidate(job["id"], expected_revision=value["revision"])
                assert published["status"] == "done", published
                experiences = continuity.experiences.list(agent_id)["items"]
                assert len(experiences) == 1
                event = experiences[0]
                assert datetime.fromisoformat(event["occurred_start"]) == datetime.fromisoformat(original[0]["created_at"])
                assert datetime.fromisoformat(event["occurred_end"]) == datetime.fromisoformat(original[-1]["created_at"])
                assert "2099" not in event["occurred_start"] + event["occurred_end"]
                assert app.conversations.messages(cid) == original
                with app.database.transaction() as db:
                    count = db.execute("SELECT COUNT(*) FROM continuity_experience_sources").fetchone()[0]
                assert count == len(original), "100% independent source cover"
            finally:
                await runner.aclose()
    asyncio.run(scenario())
