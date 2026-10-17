"""Independent M2-v2 integration and recovery checks.

These tests deliberately cross the public service, real durable runtime,
SQLite persistence and the native model boundary.  The protocol fixtures emit
actual SSE bytes and are consumed by ``ModelGateway`` through MockTransport;
there is no test-only event decoder or network fallback.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fixtures.m2_v2.crashpoints import crash_at
from fixtures.m2_v2.protocols import (
    MockModelTransport,
    profile,
    response_for,
    tool_response_for,
)

from lingnexus.common.errors import Conflict
from lingnexus.models import APIFamily
from lingnexus.runtime import ChatRequest, ChatRuntime


def _configure(app, family: APIFamily):
    item = app.profiles.save(
        f"fixture-{family.value}",
        profile(family, model=f"fixture-{family.value}"),
        api_key="test-runtime-key",
    )
    app.profiles.select(item["id"], None)
    return item["id"]


def _grant(app, agent_id: str, folder: Path) -> None:
    folder.mkdir(exist_ok=True)
    app.permissions.configure(
        agent_id,
        readable=[folder],
        writable=[folder],
        expected_revision=0,
    )


def _run(app, transport: MockModelTransport, request: ChatRequest) -> tuple[dict, list[dict]]:
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(request)
                events = [event async for event in runtime.events(run["id"])]
                return runtime.get_run(run["id"]), events
            finally:
                await runtime.aclose()

    return asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_native_tool_exchange_reaches_completion_in_real_request_bodies(
    app, agent_id, tmp_path, family
):
    """Every supported family performs one tool step and one final answer."""
    _configure(app, family)
    folder = tmp_path / "granted"
    _grant(app, agent_id, folder)
    target = folder / f"{family.value}.txt"
    transport = MockModelTransport.from_responses(
        [
            tool_response_for(
                family,
                [("write-1", "create_file", {"path": str(target), "text": "正文哨兵-42"})],
            ),
            response_for(family, "已完成并核对。"),
        ]
    )

    run, events = _run(
        app,
        transport,
        ChatRequest(
            agent_id=agent_id,
            message="写一个文件再确认",
            request_id=f"protocol-{family.value}",
        ),
    )

    assert run["status"] == "completed", events
    assert target.read_text(encoding="utf-8") == "正文哨兵-42"
    assert len(transport.requests) == 2
    assert transport.remaining() == 0
    assert transport.calls[0]["method"] == "POST"
    first = transport.requests[0]
    environment = (
        first.get("system")
        or first.get("instructions")
        or first["messages"][0]["content"]
    )
    assert folder.as_posix() in environment or str(folder) in environment

    # The second model request must contain a native result tied to the call;
    # exact field spelling is asserted per family below instead of accepting a
    # normalized transcript that could never have come from the provider.
    second = transport.requests[1]
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        assert second["messages"][-1]["role"] == "tool"
        assert second["messages"][-1]["tool_call_id"] == "write-1"
        assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "create_file"
    elif family == APIFamily.ANTHROPIC_MESSAGES:
        assert second["messages"][-1]["content"][0]["type"] == "tool_result"
        assert second["messages"][-1]["content"][0]["tool_use_id"] == "write-1"
        assert second["messages"][-2]["content"][0]["type"] == "tool_use"
    else:
        assert second["input"][-1] == {
            "type": "function_call_output",
            "call_id": "write-1",
            "output": second["input"][-1]["output"],
        }
        assert second["input"][-2]["type"] == "function_call"
        assert second["input"][-2]["call_id"] == "write-1"

    started = [event for event in events if event["event"] == "tool_started"]
    results = [event for event in events if event["event"] == "tool_result"]
    assert [event["data"]["step"] for event in started] == [1]
    assert [event["data"]["step"] for event in results] == [1]
    assert results[0]["data"]["status"] == "completed"
    assert [event["event"] for event in events].count("complete") == 1


def _continuity(app):
    from lingnexus.continuity import Continuity

    if not getattr(app, "continuity", None):
        app.continuity = Continuity(app)
    return app.continuity


def _branch(app, aid, *, objective="核对目标与未完成项", origin=None):
    continuity = _continuity(app)
    cid = app.conversations.create(aid, "主干")["id"]
    continuity.main.bind(aid, cid, 0)
    user = app.conversations.append_user(cid, "FORK_BACKGROUND_tea：继续讨论茶", request_id="fork-user")
    anchor = app.conversations.append_assistant(cid, "稍后继续原话题", request_id="fork-answer",
                                               expected_head=user["id"])
    branch = continuity.branch(aid, objective=objective, request_id="branch-once",
                               origin_message_id=origin or anchor["id"])
    return continuity, cid, branch


@pytest.mark.parametrize("family", list(APIFamily))
def test_main_and_real_work_overlap_but_same_conversation_is_exclusive(app, agent_id, family):
    """N2/N3: actual native HTTP calls overlap on two real conversations."""
    _configure(app, family)
    continuity, cid, branch = _branch(app, agent_id)
    before = app.conversations.messages(cid)
    same = continuity.branch(agent_id, objective=branch["objective"], request_id="branch-once",
                             origin_message_id=branch["origin_message_id"])
    assert same["id"] == branch["id"]
    with pytest.raises(Conflict):
        continuity.branch(agent_id, objective="冲突目标", request_id="branch-once")
    bodies = []

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            if "MAIN_ACTIVE_42" in request.content.decode():
                entered.set()
                await release.wait()
                text = "主干继续聊茶"
            else:
                text = "WORK_DETAIL_DO_NOT_LEAK：工作运行结束，尚未显式finish"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=response_for(family, text))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                main = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=cid,
                                      message="MAIN_ACTIVE_42", request_id="main-active"))
                await asyncio.wait_for(entered.wait(), 10)
                main_head = app.conversations.read(cid)["head_id"]
                with pytest.raises(Conflict):
                    runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=cid,
                                   message="同会话抢占", request_id="same-conversation"))
                work = runtime.submit(ChatRequest(agent_id=agent_id,
                                      conversation_id=branch["conversation_id"],
                                      message=branch["objective"], request_id="work-concurrent"))
                async with asyncio.timeout(10):
                    work_events = [event async for event in runtime.events(work["id"])]
                assert runtime.get_run(work["id"])["status"] == "completed", work_events
                assert runtime.get_run(main["id"])["status"] == "running"
                assert app.conversations.read(cid)["head_id"] == main_head
                assert len(bodies) == 2, "work must reach HTTP while main HTTP is held"
                assert "FORK_BACKGROUND_tea" in json.dumps(bodies[1], ensure_ascii=False)
                assert "MAIN_ACTIVE_42" not in json.dumps(bodies[1], ensure_ascii=False)
                assert continuity.work(branch["id"])["status"] != "archived", "model stop is not finish"
                release.set()
                async with asyncio.timeout(10):
                    main_events = [event async for event in runtime.events(main["id"])]
                assert runtime.get_run(main["id"])["status"] == "completed", main_events
                stored = app.conversations.messages(cid)
                assert stored[:len(before)] == before
                assert all("WORK_DETAIL_DO_NOT_LEAK" not in m["text"] for m in stored)
            finally:
                release.set()
                await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_two_live_epochs_effect_completion_restart_preserves_handoff_without_old_details(
    app, agent_id, tmp_path, app_factory, monkeypatch, family
):
    """N6/N7: compression happens inside a live run, before a new write effect."""
    from lingnexus.runtime.compaction import CompactionService
    from lingnexus.runtime.scope import ChatToolScope

    item = app.profiles.save("epochs", profile(family, context_window_tokens=200_000,
                             max_output_tokens=1000), api_key="test-runtime-key")
    app.profiles.select(item["id"], None)
    folder = tmp_path / "epoch-synthetic"
    _grant(app, agent_id, folder)
    markers = ("OLD_DETAIL_EPOCH_0_7f2c", "OLD_DETAIL_EPOCH_1_d3e9")
    inputs = [folder / f"detail-{i}.txt" for i in range(2)]
    for i, (path, marker) in enumerate(zip(inputs, markers, strict=True)):
        # Distinguish useful bounded evidence from obsolete process detail.
        # Both must be present in the initial real read, but only the evidence
        # may be retained in the handoff after cutting the private chain.
        path.write_text(f"VERIFIED_PROGRESS_{i}：输入已完成校验。\n" + "x" * 50_000
                        + marker + "x" * 50_000, encoding="utf-8")
    objective = "OBJECTIVE_KEEP_9a：核验两份输入；已验证进度留存；TODO_KEEP_8b：核对交付"
    _, main_cid, branch = _branch(app, agent_id, objective=objective)
    before_main = app.conversations.messages(main_cid)
    CompactionService(app).configure(agent_id, expected_revision=0, enabled=True,
        profile_modes={item["id"]: "safe_epoch_restart"}, trigger_ratio=.55, target_ratio=.45)
    # Each real read creates enough native input to cross the deterministic byte
    # estimate. No pressure/epoch method is mocked or manually called.
    target = folder / "effect-once.txt"
    writes = []
    original_write = ChatToolScope.write

    def counted_write(self, raw, text, **kwargs):
        if Path(raw) == target:
            writes.append(raw)
        return original_write(self, raw, text, **kwargs)

    monkeypatch.setattr(ChatToolScope, "write", counted_write)
    transport = MockModelTransport.from_responses([
        tool_response_for(family, [("read-old-0", "read_file", {"path": str(inputs[0])})]),
        tool_response_for(family, [("read-old-1", "read_file", {"path": str(inputs[1])})]),
        tool_response_for(family, [("write-once", "create_file", {"path": str(target), "text": "effect-one"})]),
        response_for(family, "工作本轮已完成，仍需核对交付。"),
    ])
    run, events = _run(app, transport, ChatRequest(agent_id=agent_id,
                conversation_id=branch["conversation_id"], message=objective, request_id="two-live-epochs"))
    assert run["status"] == "completed", events
    with app.database.transaction() as db:
        archived = db.execute("SELECT epoch,state FROM chat_context_epochs WHERE run_id=? ORDER BY epoch", (run["id"],)).fetchall()
        effects = db.execute("SELECT step,call_id,status FROM chat_tool_effects WHERE run_id=? ORDER BY step", (run["id"],)).fetchall()
    assert len(archived) >= 2, "must compact at least twice while work is still running"
    assert all(m in "".join(row["state"] for row in archived) for m in markers)
    assert len(writes) == 1
    assert target.read_text(encoding="utf-8") == "effect-one"
    assert len([r for r in effects if r["call_id"] == "write-once" and r["status"] == "completed"]) == 1
    assert [r["step"] for r in effects] == [1, 2, 3], "epoch must not reset global step"
    assert app.conversations.messages(main_cid) == before_main
    # Even before restart, the request for the new write contains the handoff,
    # excludes both sealed detail blocks and is smaller than the old read body.
    handoff_body = json.dumps(transport.requests[2], ensure_ascii=False)
    assert "OBJECTIVE_KEEP_9a" in handoff_body and "TODO_KEEP_8b" in handoff_body
    assert 'read-old-0' in handoff_body and 'read-old-1' in handoff_body
    assert "VERIFIED_PROGRESS_0" in handoff_body and "VERIFIED_PROGRESS_1" in handoff_body
    leaked = [(m, handoff_body[max(0, handoff_body.find(m)-100):handoff_body.find(m)+len(m)+100])
              for m in markers if m in handoff_body]
    assert not leaked, leaked

    restarted = app_factory(app.database.path)
    try:
        _continuity(restarted)
        restarted.profiles.save("epochs", profile(family, context_window_tokens=200_000,
                    max_output_tokens=1000), api_key="test-runtime-key", profile_id=item["id"])
        next_transport = MockModelTransport.from_responses([response_for(family, "继续核对交付")])
        next_run, next_events = _run(restarted, next_transport, ChatRequest(agent_id=agent_id,
            conversation_id=branch["conversation_id"], message="继续下一轮", request_id="after-restart"))
        assert next_run["status"] == "completed", next_events
        actual = json.dumps(next_transport.requests[0], ensure_ascii=False)
        assert all(marker not in actual for marker in markers)
        assert "OBJECTIVE_KEEP_9a" in actual
        assert "TODO_KEEP_8b" in actual
        assert "VERIFIED_PROGRESS_0" in actual and "VERIFIED_PROGRESS_1" in actual
        assert "read-old-0" in actual and "read-old-1" in actual
        assert "completed" in actual, "verified step facts must survive restart/replay"
        assert len(writes) == 1, "next turn must not replay a completed file effect"
    finally:
        restarted.close()


@pytest.mark.parametrize("family", list(APIFamily))
def test_coordinator_finish_waits_for_main_then_return_a_b_revocation_removes_derived_body(
    app, agent_id, family
):
    """N5/N14: real finish tool→outbox→tool-free A→B→revoke, no private cache."""
    from lingnexus.continuity.coordinator import BranchCoordinator

    _configure(app, family)
    continuity, cid, branch = _branch(app, agent_id, objective="RETURN_FACT_451：整理合成结果")
    bodies = []

    async def scenario():
        main_entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            raw = request.content.decode()
            if "ANSWER_A_REQUEST" in raw:
                answer = "DERIVED_A_624：基于该回归的回答A"
                if "ANSWER_B_REQUEST" in raw:
                    answer = "DERIVED_B_925：基于回答A的回答B"
                if "AFTER_REVOKE_REQUEST" in raw:
                    answer = "来源已撤回"
                stream = response_for(family, answer)
            elif "MAIN_WAIT_X" in raw:
                main_entered.set()
                await release.wait()
                stream = response_for(family, "主干仍能继续聊天")
            elif len([b for b in bodies if "RETURN_FACT_451" in json.dumps(b, ensure_ascii=False)]) == 1:
                stream = tool_response_for(family, [("finish-intent", "work_branch_finish", {})])
            else:
                stream = response_for(family, "WORK_FINAL_765：工具已完成，交付未独立核验")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            try:
                main = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=cid,
                    message="MAIN_WAIT_X", request_id="outbox-main"))
                await asyncio.wait_for(main_entered.wait(), 10)
                active_head = app.conversations.read(cid)["head_id"]
                frozen_http = json.dumps(bodies[0], sort_keys=True)
                await coordinator.tick()
                working = continuity.work(branch["id"])
                assert working["run_id"]
                async with asyncio.timeout(10):
                    events = [e async for e in runtime.events(working["run_id"])]
                assert runtime.get_run(working["run_id"])["status"] == "completed", events
                assert any(e["event"] == "tool_result" and e["data"]["name"] == "work_branch_finish"
                           and e["data"]["status"] == "completed" for e in events)
                await coordinator.tick()
                assert continuity.work(branch["id"])["archive_id"]
                assert continuity.work(branch["id"])["return_state"] == "pending"
                assert runtime.get_run(main["id"])["status"] == "running"
                assert app.conversations.read(cid)["head_id"] == active_head
                assert json.dumps(bodies[0], sort_keys=True) == frozen_http
                release.set()
                async with asyncio.timeout(10):
                    async for _ in runtime.events(main["id"]):
                        pass
                before_return = app.conversations.read(cid)["head_id"]
                await coordinator.tick()
                report = app.conversations.messages(cid)[-1]
                assert report["parent_id"] == before_return
                assert report["id"] != branch["origin_message_id"]
                assert "RETURN_FACT_451" in report["text"]
                assert "completed_unverified" in report["text"]
                for _ in range(3):
                    await coordinator.tick()
                    continuity.returns.publish()
                assert app.conversations.read(cid)["head_id"] == report["id"]
                with app.database.transaction() as db:
                    assert db.execute("SELECT COUNT(*) FROM continuity_branch_returns WHERE branch_id=?", (branch["id"],)).fetchone()[0] == 1

                for tag in ("ANSWER_A_REQUEST", "ANSWER_B_REQUEST"):
                    run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=cid,
                        message=tag, request_id=tag))
                    async with asyncio.timeout(10):
                        async for _ in runtime.events(run["id"]):
                            pass
                    assert runtime.get_run(run["id"])["status"] == "completed"
                    with app.database.transaction() as db:
                        assert db.execute("SELECT COUNT(*) FROM continuity_answer_dependencies WHERE run_id=?", (run["id"],)).fetchone()[0] >= 1
                        assert db.execute("SELECT COUNT(*) FROM chat_tool_effects WHERE run_id=?", (run["id"],)).fetchone()[0] == 0
                experience = continuity.experiences.list(agent_id, kind="work_branch")["items"][0]
                with app.database.transaction() as db:
                    db.execute("DELETE FROM chat_run_checkpoints")
                continuity.revoke_experience(experience["id"], expected_revision=experience["revision"], agent_id=agent_id)
                run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=cid,
                    message="AFTER_REVOKE_REQUEST", request_id="after-revoke"))
                async with asyncio.timeout(10):
                    async for _ in runtime.events(run["id"]):
                        pass
                actual = json.dumps(bodies[-1], ensure_ascii=False)
                for marker in ("RETURN_FACT_451", "WORK_FINAL_765", "DERIVED_A_624", "DERIVED_B_925"):
                    assert marker not in actual, f"revoked derived text reached actual HTTP: {marker}"
            finally:
                release.set()
                await coordinator.aclose()
                await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("point", ["archive_before_outbox", "publish_after_message", "publish_after_outbox"])
def test_return_crash_transaction_rolls_back_then_restart_publishes_once(
    app, agent_id, app_factory, point
):
    family = APIFamily.OPENAI_CHAT_COMPLETIONS
    _configure(app, family)
    continuity, cid, branch = _branch(app, agent_id)
    run, events = _run(app, MockModelTransport.from_responses([response_for(family, "合成完成记录")]),
        ChatRequest(agent_id=agent_id, conversation_id=branch["conversation_id"],
                    message=branch["objective"], request_id="crash-work"))
    assert run["status"] == "completed", events
    current = continuity.work(branch["id"])
    continuity.request_finish(branch["id"], current["revision"])
    if point != "archive_before_outbox":
        continuity.returns.archive(branch["id"])
    before = app.conversations.messages(cid)
    with crash_at(app.database, point), pytest.raises(sqlite3.IntegrityError, match="synthetic crash point"):
        if point == "archive_before_outbox":
            continuity.returns.archive(branch["id"])
        else:
            continuity.returns.publish()
    assert app.conversations.messages(cid) == before
    with app.database.transaction() as db:
        if point == "archive_before_outbox":
            # A live work experience exists when the branch is created. The
            # crash must roll back its archive mutation and every outbox row,
            # without deleting that independently registered live experience.
            experiences = db.execute(
                "SELECT * FROM continuity_experiences WHERE branch_id=?", (branch["id"],)
            ).fetchall()
            assert len(experiences) == 1 and experiences[0]["coverage"] == "live"
            assert db.execute("SELECT COUNT(*) FROM continuity_branch_returns").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM continuity_branch_returns WHERE status='published'").fetchone()[0] == 0
    restarted = app_factory(app.database.path)
    try:
        c = _continuity(restarted)
        c.returns.archive(branch["id"])
        c.returns.publish()
        c.returns.publish()
        after = restarted.conversations.messages(cid)
        assert len(after) == len(before) + 1
        assert after[-1]["parent_id"] == before[-1]["id"]
        with restarted.database.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM continuity_branch_returns WHERE status='published'").fetchone()[0] == 1
    finally:
        restarted.close()

