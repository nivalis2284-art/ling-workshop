"""Maintenance regressions crossing work admission, native payload and SQLite."""

import asyncio
import json

import httpx
import pytest
from fixtures.m2_v2.protocols import MockModelTransport, profile, response_for, tool_response_for

from lingnexus.common.errors import Conflict
from lingnexus.continuity.coordinator import BranchCoordinator
from lingnexus.continuity.work import append_task_state
from lingnexus.models import APIFamily
from lingnexus.runtime import ChatRequest, ChatRuntime
from lingnexus.runtime.checkpoints import CheckpointLimit


def setup_work(app, agent_id, family=APIFamily.OPENAI_CHAT_COMPLETIONS):
    saved = app.profiles.save("maintenance", profile(family), api_key="maintenance-key")
    app.profiles.select(saved["id"], None)
    main = app.continuity.bind_main(agent_id, None, 0)
    branch = app.continuity.branch(agent_id, objective="核对旧数据", request_id="maintenance-work")
    assert main["conversation"]["id"] == branch["main_conversation_id"]
    return branch


async def drain(runtime, run_id):
    task = runtime._tasks.get(run_id)
    events = [event async for event in runtime.events(run_id)]
    if task:
        await task
    return events


@pytest.mark.parametrize("family", list(APIFamily))
def test_full_progress_does_not_break_wake_or_round_commit(app, agent_id, family):
    branch = setup_work(app, agent_id, family)
    with app.database.transaction() as db:
        append_task_state(db, branch["id"], 2, objective=branch["objective"],
                          progress=[f"事实-{i}" for i in range(64)],
                          artifacts=["保留产物"], sources=["保留来源"], grants={}, run_id=None)
        db.execute("UPDATE continuity_work_branches SET task_state_revision=2 WHERE id=?", (branch["id"],))
        db.execute("UPDATE continuity_work_episodes SET task_state_revision=2 WHERE id=?", (branch["current_episode_id"],))
    transport = MockModelTransport.from_responses([response_for(family, "本轮核对完成")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                await BranchCoordinator(app, runtime).tick()
                run_id = app.continuity.work(branch["id"])["run_id"]
                events = await drain(runtime, run_id)
                assert runtime.get_run(run_id)["status"] == "completed", events
                assert not any(e["data"].get("code") == "runtime_diagnostic" for e in events)
                state = app.continuity.task_state(branch["id"])
                assert len(state["progress"]) == 64
                assert "本轮核对完成" in state["progress"][-1]
                assert state["artifacts"] == ["保留产物"]
                assert state["sources"] == ["保留来源"]
                assert app.continuity.work(branch["id"])["status"] == "paused"
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_guidance_over_request_limit_drains_across_episodes(app, agent_id):
    branch = setup_work(app, agent_id)
    for i in range(65):
        app.continuity.guide(branch["id"], f"GUIDANCE_{i:03}", f"guide-{i}")
    transport = MockModelTransport.from_responses([
        response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "first batch"),
        response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "second batch"),
    ])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            try:
                for expected in (64, 65):
                    await coordinator.tick()
                    run_id = [e["run_id"] for e in app.continuity.episodes(branch["id"]) if e["run_id"]][-1]
                    await drain(runtime, run_id)
                    guides = app.continuity.guidance(branch["id"])
                    assert sum(g["consumed_at"] is not None for g in guides) == expected
                await coordinator.tick()
                assert len(transport.requests) == 2
                assert app.continuity.work(branch["id"])["status"] == "paused"
                episodes = app.continuity.episodes(branch["id"])
                assert len(episodes) == 2
                assert episodes[1]["predecessor_id"] == episodes[0]["id"]
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_direct_work_chat_is_bound_and_can_continue_paused_work(app, agent_id, family):
    branch = setup_work(app, agent_id, family)
    transport = MockModelTransport.from_responses([response_for(family, "一"), response_for(family, "二")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                for i in range(2):
                    run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=branch["conversation_id"],
                                                     message=f"所有者直接交流{i}", request_id=f"direct-{i}"))
                    assert runtime.work_episode(run["id"]) is not None
                    assert app.continuity.work(branch["id"])["run_id"] == run["id"]
                    await drain(runtime, run["id"])
                    assert app.continuity.work(branch["id"])["status"] == "paused"
                await BranchCoordinator(app, runtime).tick()
                assert len(transport.requests) == 2
                assert len(app.continuity.episodes(branch["id"])) == 2
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_claim_without_run_keeps_episode_admissible(app, agent_id):
    branch = setup_work(app, agent_id)
    with app.database.transaction() as db:
        db.execute("UPDATE continuity_work_branches SET status='starting' WHERE id=?", (branch["id"],))
    transport = MockModelTransport.from_responses([response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "recovered claim")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                await BranchCoordinator(app, runtime).tick()
                current = app.continuity.work(branch["id"])
                assert current["run_id"] is not None, current
                await drain(runtime, current["run_id"])
                assert runtime.get_run(current["run_id"])["status"] == "completed"
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_initial_guidance_receipt_rolls_back_when_checkpoint_cannot_commit(app, agent_id, monkeypatch):
    branch = setup_work(app, agent_id)
    app.continuity.guide(branch["id"], "MUST_REMAIN_UNREAD", "checkpoint-guide")
    transport = MockModelTransport.from_responses([])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            def fail_save(*args, **kwargs):
                raise CheckpointLimit("injected checkpoint failure")
            monkeypatch.setattr(runtime.checkpoints, "save", fail_save)
            try:
                await BranchCoordinator(app, runtime).tick()
                run_id = app.continuity.work(branch["id"])["run_id"]
                await drain(runtime, run_id)
                assert app.continuity.guidance(branch["id"])[0]["consumed_at"] is None
                assert transport.requests == []
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_caller_cannot_acknowledge_guidance_absent_from_input(app, agent_id):
    branch = setup_work(app, agent_id)
    app.continuity.guide(branch["id"], "NEVER_IN_PAYLOAD", "forged-receipt")
    guide = app.continuity.guidance(branch["id"])[0]

    async def scenario():
        runtime = ChatRuntime(app)
        try:
            with pytest.raises(Conflict):
                runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=branch["conversation_id"],
                                           message="different input", request_id="forged-input",
                                           work_episode_id=branch["current_episode_id"], work_guidance_ids=[guide["id"]]))
            with app.database.transaction() as db:
                assert db.execute("SELECT count(*) FROM chat_runs").fetchone()[0] == 0
            assert app.continuity.guidance(branch["id"])[0]["consumed_at"] is None
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_multibyte_final_text_does_not_rollback_success(app, agent_id):
    branch = setup_work(app, agent_id)
    transport = MockModelTransport.from_responses([response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "😀" * 600)])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                await BranchCoordinator(app, runtime).tick()
                run_id = app.continuity.episodes(branch["id"])[0]["run_id"]
                events = await drain(runtime, run_id)
                assert runtime.get_run(run_id)["status"] == "completed", events
                assert not any(e["data"].get("code") == "runtime_diagnostic" for e in events)
                assert all(len(p.encode()) <= 2048 for p in app.continuity.task_state(branch["id"])["progress"])
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_old_outbox_cannot_change_new_archives_return_state(app, agent_id):
    branch = setup_work(app, agent_id)
    transport = MockModelTransport.from_responses([
        response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "old archive"),
        response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "new archive"),
    ])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                for i in range(2):
                    if i:
                        row = app.continuity.work(branch["id"])
                        app.continuity.wake(branch["id"], row["revision"])
                    run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=branch["conversation_id"],
                                                     message=f"work-{i}", request_id=f"archive-{i}"))
                    await drain(runtime, run["id"])
                    row = app.continuity.work(branch["id"])
                    app.continuity.request_finish(branch["id"], row["revision"])
                    app.continuity.returns.archive(branch["id"])
                assert app.continuity.work(branch["id"])["return_state"] == "pending"
                assert len(app.continuity.returns.publish(limit=1)) == 1
                assert app.continuity.work(branch["id"])["return_state"] == "pending"
                assert len(app.continuity.returns.publish(limit=1)) == 1
                assert app.continuity.work(branch["id"])["return_state"] == "published"
                assert app.continuity.returns.publish() == []
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_work_state_reaches_direct_chat_even_without_frozen_main_messages(app, agent_id, family):
    branch = setup_work(app, agent_id, family)
    with app.database.transaction() as db:
        append_task_state(db, branch["id"], 2, objective=branch["objective"],
                          decisions=["DURABLE_DECISION"], progress=["DURABLE_PROGRESS"],
                          artifacts=["DURABLE_ARTIFACT"], sources=["DURABLE_SOURCE"], grants={}, run_id=None)
        db.execute("UPDATE continuity_work_branches SET task_state_revision=2 WHERE id=?", (branch["id"],))
        db.execute("UPDATE continuity_work_episodes SET task_state_revision=2 WHERE id=?", (branch["current_episode_id"],))
    transport = MockModelTransport.from_responses([response_for(family, "已读状态")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=branch["conversation_id"],
                                                 message="继续", request_id="direct-state"))
                await drain(runtime, run["id"])
                assert runtime.get_run(run["id"])["status"] == "completed"
                body = json.dumps(transport.requests[-1])
                for marker in ("DURABLE_DECISION", "DURABLE_PROGRESS", "DURABLE_ARTIFACT", "DURABLE_SOURCE"):
                    assert marker in body
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_fork_packet_respects_utf8_byte_budget(app, agent_id):
    from lingnexus.continuity.projection import MAX_BACKGROUND_BYTES, fork_messages

    branch = setup_work(app, agent_id)
    with app.database.transaction() as db:
        append_task_state(db, branch["id"], 2, objective=branch["objective"],
                          decisions=["😀" * 500 for _ in range(12)], grants={}, run_id=None)
    projected, _ = fork_messages(app.continuity, agent_id, branch["conversation_id"], [])
    assert projected
    assert len(projected[0]["text"].encode()) <= MAX_BACKGROUND_BYTES


def test_inflight_native_replay_does_not_take_new_guidance(app, agent_id):
    branch = setup_work(app, agent_id)

    async def scenario():
        runtime = ChatRuntime(app)
        try:
            run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=branch["conversation_id"],
                                             message="原始输入", request_id="inflight-guard"))
            runtime.guide(run["id"], "DEFER_UNTIL_READY")
            state = {"phase": "model_inflight", "payload": {"messages": []}}
            await runtime._control_gate(run["id"], state)
            assert state["payload"] == {"messages": []}
            assert runtime._control[run["id"]].guides == ["DEFER_UNTIL_READY"]
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_live_guidance_receipt_and_replay_payload_commit_together(app, agent_id, family, monkeypatch):
    branch = setup_work(app, agent_id, family)

    async def scenario():
        async def handler(request):
            app.continuity.guide(branch["id"], "LIVE_CHECKPOINT_GUIDE", "live-checkpoint")
            coordinator._queue_guidance(app.continuity.work_domain.row(branch["id"]))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=tool_response_for(family, [("state-before-guide", "work_branch_state", {"progress": ["工具已闭合"]})]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            save = runtime.checkpoints.save
            def fail_new_payload(run_id, state, **kwargs):
                if "LIVE_CHECKPOINT_GUIDE" in json.dumps(state["payload"]):
                    raise CheckpointLimit("injected live checkpoint failure")
                return save(run_id, state, **kwargs)
            monkeypatch.setattr(runtime.checkpoints, "save", fail_new_payload)
            try:
                await coordinator.tick()
                run_id = app.continuity.episodes(branch["id"])[0]["run_id"]
                await drain(runtime, run_id)
                assert runtime.get_run(run_id)["status"] == "failed"
                assert app.continuity.guidance(branch["id"])[0]["consumed_at"] is None
                checkpoint = runtime.checkpoints.load(run_id)
                assert checkpoint["phase"] == "ready"
                assert not checkpoint["pending"]
                assert "LIVE_CHECKPOINT_GUIDE" not in json.dumps(checkpoint["payload"])
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_invalid_queued_fork_does_not_starve_another_agents_work(app, agent_id):
    first = setup_work(app, agent_id)
    message = app.conversations.append_user(first["main_conversation_id"], "original", request_id="source")
    # Branches without excerpts still bind to the main epoch; changing the
    # source tree must fence this branch, without aborting the whole scheduler.
    app.conversations.edit_user(first["main_conversation_id"], message["id"], "edited", request_id="source-edit")
    second = setup_work(app, app.create_agent("另一灵"))
    transport = MockModelTransport.from_responses([response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "other agent works")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            try:
                await BranchCoordinator(app, runtime).tick()
                assert app.continuity.work(first["id"])["status"] == "pending_review"
                run_id = app.continuity.episodes(second["id"])[0]["run_id"]
                await drain(runtime, run_id)
                assert runtime.get_run(run_id)["status"] == "completed"
                assert len(transport.requests) == 1
            finally:
                await runtime.aclose()
    asyncio.run(scenario())


def test_coordinator_conflict_cannot_requeue_an_owner_claimed_run(app, agent_id, monkeypatch):
    branch = setup_work(app, agent_id)
    transport = MockModelTransport.from_responses([response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "owner wins claim")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            original = coordinator._wake_message
            owner_runs = []
            def interleaved_owner_submit(row, guidance):
                owner_runs.append(runtime.submit(ChatRequest(
                    agent_id=agent_id, conversation_id=branch["conversation_id"],
                    message="所有者先提交", request_id="owner-claim-wins")))
                return original(row, guidance)
            monkeypatch.setattr(coordinator, "_wake_message", interleaved_owner_submit)
            # Keep the request pending while tick handles its own stale conflict.
            original_publish = app.continuity.returns.publish
            def inspect_publish(*args, **kwargs):
                row = app.continuity.work(branch["id"])
                assert row["status"] in {"running", "paused"}, row
                assert row["run_id"] == owner_runs[0]["id"]
                return original_publish(*args, **kwargs)
            monkeypatch.setattr(app.continuity.returns, "publish", inspect_publish)
            try:
                await coordinator.tick()
                run_id = owner_runs[0]["id"]
                await drain(runtime, run_id)
                assert app.continuity.work(branch["id"])["status"] == "paused"
                assert len(app.continuity.episodes(branch["id"])) == 1
                assert len(transport.requests) == 1
            finally:
                await runtime.aclose()
    asyncio.run(scenario())
