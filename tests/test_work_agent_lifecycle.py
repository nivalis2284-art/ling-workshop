"""M2 phase one: the work branch is the durable task agent itself.

These cases cross the real service, SQLite, BranchCoordinator and native model
loop. A successor episode must reuse the same branch/conversation, carry the
previous task understanding into the next actual request, and leave old archive
bytes, effects and checkpoints untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fixtures.m2_v2.protocols import (
    MockModelTransport,
    profile,
    response_for,
    tool_response_for,
)

from lingnexus.continuity import Continuity
from lingnexus.continuity.coordinator import BranchCoordinator
from lingnexus.models import APIFamily
from lingnexus.runtime import ChatRequest, ChatRuntime


def _configure(app, family):
    item = app.profiles.save(
        f"lifecycle-{family.value}",
        profile(family, model=f"fixture-{family.value}"),
        api_key="test-lifecycle-key",
    )
    app.profiles.select(item["id"], None)


def test_migration_backfills_legacy_branch_without_rewriting_it(tmp_path):
    """An archived pre-037 branch becomes readable state; its old fields stay put."""
    from lingnexus.storage.db import Database

    path = tmp_path / "legacy.db"
    migrations = Path(__file__).parents[1] / "src/lingnexus/storage/migrations"
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)")
    for file in sorted(migrations.glob("*.sql")):
        if file.name >= "037":
            continue
        # Mirror the runner exactly: BEGIN, bundled SQL, ledger row, COMMIT.
        script = "BEGIN IMMEDIATE;\n" + file.read_text(encoding="utf-8")
        script += "\nINSERT INTO schema_migrations VALUES ('" + file.name + "'); COMMIT;"
        db.executescript(script)
    db.execute("INSERT INTO agents VALUES('legacy-agent','灵','2026-10-01T00:00:00+00:00')")
    db.execute(
        "INSERT INTO conversations(id,agent_id,title,scope,head_id,created_at) "
        "VALUES('legacy-conv','legacy-agent','旧主干','','', '2026-10-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO continuity_work_branches(id,agent_id,conversation_id,main_conversation_id,"
        "objective,status,revision,finish_requested,outcome,archive_id,archive_version,"
        "result_revision,return_state,delivery_state,return_report,members,background,grants,"
        "request_id,fingerprint,created_at,updated_at) VALUES(?,'legacy-agent','legacy-conv',"
        "'legacy-conv','旧目标','archived',1,1,'completed_unverified','old-archive',1,1,"
        "'published','delivered','旧回归','[]','[]','{}','legacy-request','legacy-fingerprint',"
        "'2026-10-01T00:00:00+00:00','2026-10-02T00:00:00+00:00')",
        ("legacy-branch",),
    )
    db.commit()
    db.close()

    # The production runner opens each migration in its own transaction. Recreate
    # that boundary so 026's explicit COMMIT helper sees the same context.
    migrated = Database(path)
    with migrated.transaction() as connection:
        branch = connection.execute(
            "SELECT status,archive_id,return_report,current_episode_id,task_state_revision "
            "FROM continuity_work_branches WHERE id='legacy-branch'"
        ).fetchone()
        episode = connection.execute(
            "SELECT status,predecessor_id,run_id FROM continuity_work_episodes "
            "WHERE branch_id='legacy-branch'"
        ).fetchone()
        state = connection.execute(
            "SELECT revision,objective FROM continuity_work_task_states WHERE branch_id='legacy-branch'"
        ).fetchone()
        archive = connection.execute(
            "SELECT experience_id,report FROM continuity_episode_archives WHERE archive_id='old-archive'"
        ).fetchone()
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert tuple(branch) == ("archived", "old-archive", "旧回归", "legacy:legacy-branch", 1)
    assert tuple(episode) == ("archived", None, None)
    assert tuple(state) == (1, "旧目标")
    assert tuple(archive) == ("work-live:legacy-branch", "旧回归")
    assert "037_work_agents.sql" in versions


def _branch(app, agent, objective="继续核对交付"):
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent, "主干")["id"]
    continuity.main.bind(agent, main, 0)
    app.conversations.append_user(main, "FORK_FIXTURE", request_id="fork-user")
    branch = continuity.branch(agent, objective=objective, request_id="one-branch")
    return continuity, branch


async def _drain(runtime, run_id):
    return [event async for event in runtime.events(run_id)]


@pytest.mark.parametrize("family", list(APIFamily))
def test_archived_branch_wakes_into_successor_with_task_state_in_real_request(
    app, agent_id, family
):
    _configure(app, family)
    continuity, branch = _branch(app, agent_id)
    transport = MockModelTransport.from_responses(
        [
            tool_response_for(
                family,
                [
                    (
                        "state-1",
                        "work_branch_state",
                        {
                            "decisions": ["保留旧数据"],
                            "unfinished": ["核对迁移完成"],
                        },
                    )
                ],
            ),
            response_for(family, "FIRST_ROUND_DONE"),
        ]
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            try:
                await coordinator.tick()
                live = continuity.work(branch["id"])
                old_run = live["run_id"]
                events = await _drain(runtime, old_run)
                assert runtime.get_run(old_run)["status"] == "completed", events
                state = continuity.task_state(branch["id"])
                assert state["decisions"] == ["保留旧数据"]
                assert "核对迁移完成" in state["unfinished"]

                current = continuity.work(branch["id"])
                continuity.request_finish(branch["id"], current["revision"])
                await coordinator.tick()
                archived = continuity.work(branch["id"])
                assert archived["status"] == "archived"
                old_archives = continuity.archives(branch["id"])
                assert len(old_archives) == 1
                old_archive_hash = hashlib.sha256(
                    json.dumps(old_archives[0], sort_keys=True).encode()
                ).hexdigest()
                with app.database.transaction() as db:
                    old_effects = db.execute(
                        "SELECT count(*) FROM chat_tool_effects WHERE run_id=?", (old_run,)
                    ).fetchone()[0]
                    old_checkpoint = db.execute(
                        "SELECT state FROM chat_run_checkpoints WHERE run_id=?", (old_run,)
                    ).fetchone()[0]

                woken = continuity.wake(branch["id"], archived["revision"])
                assert woken["id"] == archived["id"]
                assert woken["conversation_id"] == archived["conversation_id"]
                episodes = continuity.episodes(branch["id"])
                assert len(episodes) == 2
                assert episodes[1]["predecessor_id"] == episodes[0]["id"]
                assert episodes[1]["status"] == "active"
                assert episodes[1]["run_id"] is None

                transport.responses.append(response_for(family, "SECOND_ROUND_CONTINUES"))
                before = len(transport.requests)
                await coordinator.tick()
                successor = continuity.work(branch["id"])
                assert successor["run_id"] and successor["run_id"] != old_run
                next_events = await _drain(runtime, successor["run_id"])
                assert runtime.get_run(successor["run_id"])["status"] == "completed", next_events

                assert len(transport.requests) == before + 1
                body = json.dumps(transport.requests[-1], ensure_ascii=False)
                assert "继续核对交付" in body
                assert "保留旧数据" in body
                assert "核对迁移完成" in body
                assert body.index("同一子 Agent") < body.index("FORK_FIXTURE")

                assert continuity.archives(branch["id"]) == old_archives
                assert hashlib.sha256(
                    json.dumps(continuity.archives(branch["id"])[0], sort_keys=True).encode()
                ).hexdigest() == old_archive_hash
                with app.database.transaction() as db:
                    assert db.execute(
                        "SELECT count(*) FROM chat_tool_effects WHERE run_id=?", (old_run,)
                    ).fetchone()[0] == old_effects
                    assert db.execute(
                        "SELECT state FROM chat_run_checkpoints WHERE run_id=?", (old_run,)
                    ).fetchone()[0] == old_checkpoint
            finally:
                await coordinator.aclose()
                await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("family", list(APIFamily))
def test_guidance_queued_after_round_is_consumed_in_same_branch_successor(
    app, agent_id, family
):
    _configure(app, family)
    continuity, branch = _branch(app, agent_id)
    transport = MockModelTransport.from_responses([response_for(family, "first")])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            try:
                await coordinator.tick()
                first = continuity.work(branch["id"])
                await _drain(runtime, first["run_id"])
                paused = continuity.work(branch["id"])
                assert paused["status"] == "paused"
                queued = continuity.guide(
                    branch["id"], "GUIDE_AFTER_DONE：先核对旧数据，再继续。", "guide-after-done"
                )
                assert queued["status"] == "queued"
                episodes = continuity.episodes(branch["id"])
                assert len(episodes) == 2
                assert episodes[1]["predecessor_id"] == episodes[0]["id"]
                with app.database.transaction() as db:
                    guidance = db.execute(
                        "SELECT id,consumed_at FROM continuity_work_guidance "
                        "WHERE branch_id=? ORDER BY created_at DESC LIMIT 1",
                        (branch["id"],),
                    ).fetchone()
                assert guidance["consumed_at"] is None

                transport.responses.append(response_for(family, "continued"))
                before = len(transport.requests)
                await coordinator.tick()
                second = continuity.work(branch["id"])
                events = await _drain(runtime, second["run_id"])
                assert runtime.get_run(second["run_id"])["status"] == "completed", events
                assert len(transport.requests) == before + 1
                assert "GUIDE_AFTER_DONE" in json.dumps(
                    transport.requests[-1], ensure_ascii=False
                )
                with app.database.transaction() as db:
                    consumed = db.execute(
                        "SELECT consumed_at FROM continuity_work_guidance WHERE id=?",
                        (guidance["id"],),
                    ).fetchone()[0]
                assert consumed is not None
                assert continuity.work(branch["id"])["conversation_id"] == first["conversation_id"]
            finally:
                await coordinator.aclose()
                await runtime.aclose()

    asyncio.run(scenario())


def test_unconsumed_delivery_is_requeued_after_process_restart(app, agent_id):
    """A crash between guide handoff and injection must not lose the request."""
    _configure(app, APIFamily.OPENAI_CHAT_COMPLETIONS)
    continuity, branch = _branch(app, agent_id)

    async def scenario():
        transport = MockModelTransport.from_responses(
            [response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "first")]
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
            runtime = ChatRuntime(app, client=client)
            coordinator = BranchCoordinator(app, runtime)
            try:
                await coordinator.tick()
                first = continuity.work(branch["id"])
                await _drain(runtime, first["run_id"])
                queued = continuity.guide(branch["id"], "RESTART_GUIDE", "restart-guide")
                with app.database.transaction() as db:
                    db.execute(
                        "UPDATE continuity_work_guidance SET delivery='delivered',delivered_at=? "
                        "WHERE branch_id=? AND consumed_at IS NULL",
                        ("2026-10-18T00:00:00+00:00", branch["id"]),
                    )
                assert queued["status"] == "queued"
                # This is the crash boundary: no live _RunControl survives.
                await runtime.aclose()
                restarted_transport = MockModelTransport.from_responses(
                    [response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "continued")]
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(restarted_transport.handler)
                ) as client2:
                    restarted = ChatRuntime(app, client=client2)
                    coordinator2 = BranchCoordinator(app, restarted)
                    try:
                        await coordinator2.tick()
                        second = continuity.work(branch["id"])
                        assert second["run_id"] and second["run_id"] != first["run_id"]
                        await _drain(restarted, second["run_id"])
                        assert "RESTART_GUIDE" in json.dumps(
                            restarted_transport.requests[0], ensure_ascii=False
                        )
                    finally:
                        await coordinator2.aclose()
                        await restarted.aclose()
            finally:
                await coordinator.aclose()

    asyncio.run(scenario())


def test_pause_intent_survives_runtime_restart_and_blocks_recovery(app, agent_id):
    """A requested pause is durable; a fresh process must not resume it silently."""
    _configure(app, APIFamily.OPENAI_CHAT_COMPLETIONS)
    reached = asyncio.Event()

    async def handler(request):
        reached.set()
        await asyncio.Event().wait()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(
                ChatRequest(agent_id=agent_id, message="pause", request_id="pause-once")
            )
            await asyncio.wait_for(reached.wait(), 5)
            runtime.pause(run["id"])
            await runtime.aclose()

        restarted = ChatRuntime(app)
        try:
            status = restarted.get_run(run["id"])
            assert status["status"] == "interrupted"
            with app.database.transaction() as db:
                intent = db.execute(
                    "SELECT intent FROM chat_run_controls WHERE run_id=?", (run["id"],)
                ).fetchone()[0]
            assert intent == "pause"
            recovered = restarted.recover(run["id"])
            assert recovered["status"] == "running"
            await asyncio.sleep(0.1)
            assert restarted.get_run(run["id"])["status"] == "running"
            # A fresh runtime can resume through the same recover gate; the
            # pause intent must be cleared only after that durable admission.
            resumed = restarted.resume(run["id"])
            assert resumed["paused"] is False
            await asyncio.sleep(0.1)
            with app.database.transaction() as db:
                intent = db.execute(
                    "SELECT intent FROM chat_run_controls WHERE run_id=?", (run["id"],)
                ).fetchone()[0]
            assert intent == "running"
        finally:
            await restarted.aclose()

    asyncio.run(scenario())
