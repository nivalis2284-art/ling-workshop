"""Guards for task-state merging, bounded history and successor binding."""

from __future__ import annotations

import json

import pytest

from lingnexus.continuity import Continuity
from lingnexus.continuity.work import HISTORY_LIMIT, parse_history_cursor
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest


def _branch(app, agent_id):
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent_id, "主干")["id"]
    continuity.main.bind(agent_id, main, 0)
    branch = continuity.branch(agent_id, objective="维护回归", request_id="maintenance-branch")
    return continuity, branch


def _seed_branch(app, agent_id, ordinal, *, guidances=0):
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent_id, "主干")["id"]
    continuity.main.bind(agent_id, main, 0)
    branch = continuity.branch(
        agent_id, objective=f"维护回归 {ordinal}", request_id=f"maintenance-{ordinal}"
    )
    episode = branch["current_episode_id"]
    with app.database.transaction() as db:
        for number in range(guidances):
            db.execute(
                "INSERT INTO continuity_work_guidance(id,branch_id,request_id,fingerprint,text,"
                "delivery,created_at,episode_id) VALUES(?,?,?,?,?,'queued',?,?)",
                (f"guide-{ordinal}-{number}", branch["id"], f"guide-{ordinal}-{number}",
                 f"fingerprint-{ordinal}-{number}", "test", f"2026-10-18T00:00:{number:02d}Z",
                 episode),
            )
        if guidances:
            db.execute(
                "UPDATE continuity_work_guidance SET consumed_at=? WHERE branch_id=? AND id=?",
                ("2026-10-18T00:01:00Z", branch["id"], f"guide-{ordinal}-0"),
            )
    return continuity, branch, episode


def _paused_branch(app, agent_id):
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent_id, "主干")["id"]
    continuity.main.bind(agent_id, main, 0)
    branch = continuity.branch(agent_id, objective="维护回归", request_id="paused-branch")
    with app.database.transaction() as db:
        db.execute(
            "UPDATE continuity_work_episodes SET status='paused',closed_at=? "
            "WHERE id=?", ("2026-10-18T00:02:00Z", branch["current_episode_id"]),
        )
        db.execute(
            "UPDATE continuity_work_branches SET status='paused',run_id=NULL WHERE id=?",
            (branch["id"],),
        )
    return continuity, continuity.work(branch["id"]), branch["current_episode_id"]


def _add_history(app, branch, count):
    with app.database.transaction() as db:
        branch_id = branch["id"]
        for number in range(count):
            revision = 1000 + number
            db.execute(
                "INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,"
                "progress,unfinished,verified,unknowns,artifacts,sources,grants,digest,created_at) "
                "VALUES(?,?,?,'[]','[]','[]','[]','[]','[]','[]','{}','fixture',?)",
                (branch_id, revision, branch["objective"], f"2026-10-18T00:00:{number % 60:02d}Z"),
            )
            db.execute(
                "INSERT INTO continuity_work_episodes(id,branch_id,ordinal,status,opened_at,source_epoch) "
                "VALUES(?,?,?,'paused',?,0)",
                (f"episode-fixture-{number}", branch_id, 1000 + number, "2026-10-18T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO continuity_episode_archives(archive_id,branch_id,episode_id,experience_id,"
                "outcome,archive_version,report,created_at) VALUES(?,?,?,?, 'completed_unverified',1,'fixture',?)",
                (f"archive-fixture-{number}", branch_id, f"episode-fixture-{number}",
                 f"work-archive:fixture-{number}", f"2026-10-18T00:00:{number % 60:02d}Z"),
            )


def test_successor_rebinds_only_unconsumed_guidance(app, agent_id):
    continuity, branch, predecessor = _paused_branch(app, agent_id)
    with app.database.transaction() as db:
        for number in range(3):
            db.execute(
                "INSERT INTO continuity_work_guidance(id,branch_id,request_id,fingerprint,text,"
                "delivery,created_at,episode_id) VALUES(?,?,?,?,?,'delivered',?,?)",
                (f"guide-{number}", branch["id"], f"guide-{number}", f"fingerprint-{number}",
                 "test", f"2026-10-18T00:00:0{number}Z", predecessor),
            )
        db.execute(
            "UPDATE continuity_work_guidance SET consumed_at=? WHERE branch_id=? AND id='guide-0'",
            ("2026-10-18T00:01:00Z", branch["id"]),
        )
    woken = continuity.wake(branch["id"], branch["revision"])
    successor = woken["current_episode_id"]
    assert successor != predecessor
    with app.database.transaction() as db:
        rows = db.execute(
            "SELECT id,episode_id,delivery,consumed_at FROM continuity_work_guidance "
            "WHERE branch_id=? ORDER BY id", (branch["id"],)
        ).fetchall()
    assert rows[0]["episode_id"] == predecessor
    assert rows[0]["consumed_at"] is not None
    assert all(row["episode_id"] == successor for row in rows[1:])
    assert all(row["delivery"] == "queued" for row in rows[1:])


def test_round_successor_uses_current_branch_revision(app, agent_id):
    profile = app.profiles.save(
        "round-profile",
        ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
                     base_url="https://fixture.invalid"),
        api_key="test-round-key",
    )
    app.profiles.select(profile["id"], None)
    continuity, branch, predecessor = _seed_branch(app, agent_id, 1)
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO continuity_work_guidance(id,branch_id,request_id,fingerprint,text,"
            "delivery,created_at,episode_id) VALUES('round-guide',?,?,'fingerprint','continue',"
            "'delivered',?,?)",
            (branch["id"], "round-guide", "2026-10-18T00:00:00Z", predecessor),
        )
        db.execute(
            "UPDATE continuity_work_branches SET status='running',task_state_revision=2 "
            "WHERE id=?", (branch["id"],),
        )
        db.execute(
            "UPDATE continuity_work_episodes SET task_state_revision=2 WHERE id=?",
            (predecessor,),
        )
        db.execute(
            "INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,"
            "progress,unfinished,verified,unknowns,artifacts,sources,grants,digest,created_at) "
            "VALUES(?,2,?,'[]','[]','[]','[]','[]','[]','[]','{}','fixture',?)",
            (branch["id"], branch["objective"], "2026-10-18T00:00:01Z"),
        )
    stale = continuity.work_domain.row(branch["id"])
    with app.database.transaction() as db:
        db.execute(
            "UPDATE continuity_work_branches SET task_state_revision=3 WHERE id=?",
            (branch["id"],),
        )
    continuity.work_domain.settle_episode(stale, "paused", "round_completed")
    woken = continuity.work(branch["id"])
    assert woken["status"] == "queued"
    latest = continuity.episodes(branch["id"])[-1]
    assert latest["task_state_revision"] == 3
    with app.database.transaction() as db:
        bound = db.execute(
            "SELECT episode_id,delivery FROM continuity_work_guidance WHERE id='round-guide'"
        ).fetchone()
    assert tuple(bound) == (latest["id"], "queued")


def test_paused_guide_cannot_open_successor_past_pending_checkpoint(app, agent_id):
    profile = app.profiles.save(
        "pending-profile",
        ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
                     base_url="https://fixture.invalid"),
        api_key="test-pending-key",
    )
    app.profiles.select(profile["id"], None)
    continuity, branch, predecessor = _paused_branch(app, agent_id)
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "conversation_id,created_at) VALUES('pending-run',?,?,'fingerprint',?,'interrupted',?,?)",
            (agent_id, "pending-request", profile["id"], branch["conversation_id"],
             "2026-10-18T00:00:00Z"),
        )
        db.execute(
            "UPDATE continuity_work_episodes SET run_id=? WHERE id=?",
            ("pending-run", predecessor),
        )
        db.execute(
            "UPDATE continuity_work_branches SET run_id=? WHERE id=?",
            ("pending-run", branch["id"]),
        )
        db.execute(
            "INSERT INTO chat_run_checkpoints(run_id,state,updated_at) VALUES(?,?,?)",
            ("pending-run", '{"pending":true}', "2026-10-18T00:00:00Z"),
        )
    current = continuity.work(branch["id"])
    with pytest.raises(Exception, match="尚未解决|安全|活动|恢复"):
        continuity.guide(branch["id"], "SHOULD_NOT_OPEN", "pending-guide")
    after = continuity.work(branch["id"])
    assert after["status"] == "paused"
    assert after["current_episode_id"] == current["current_episode_id"]
    with app.database.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM continuity_work_guidance WHERE branch_id=?", (branch["id"],)
        ).fetchone()[0] == 0


def test_running_guide_keeps_append_semantics(app, agent_id):
    profile = app.profiles.save(
        "running-profile",
        ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
                     base_url="https://fixture.invalid"),
        api_key="test-running-key",
    )
    app.profiles.select(profile["id"], None)
    continuity, branch, predecessor = _seed_branch(app, agent_id, 1)
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "conversation_id,created_at) VALUES('running-run',?,?,'fingerprint',?,'running',?,?)",
            (agent_id, "running-request", profile["id"], branch["conversation_id"],
             "2026-10-18T00:00:00Z"),
        )
        db.execute(
            "UPDATE continuity_work_branches SET status='running',run_id=? WHERE id=?",
            ("running-run", branch["id"]),
        )
        db.execute(
            "UPDATE continuity_work_episodes SET run_id=? WHERE id=?",
            ("running-run", predecessor),
        )
    guided = continuity.guide(branch["id"], "LIVE_GUIDE", "running-guide")
    assert guided["current_episode_id"] == predecessor
    assert guided["status"] == "running"
    with app.database.transaction() as db:
        bound = db.execute(
            "SELECT episode_id,delivery FROM continuity_work_guidance WHERE request_id='running-guide'"
        ).fetchone()
    assert tuple(bound) == (predecessor, "queued")


def test_work_list_does_not_query_archives_per_branch(app, agent_id, monkeypatch):
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent_id, "主干")["id"]
    continuity.main.bind(agent_id, main, 0)
    with app.database.transaction() as db:
        for number in range(8):
            conversation = app.conversations.create(agent_id, f"工作 {number}")["id"]
            branch_id = f"branch-{number}"
            db.execute(
                "INSERT INTO continuity_work_branches(id,agent_id,conversation_id,main_conversation_id,"
                "objective,status,revision,request_id,fingerprint,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'paused',1,?,?,?,?)",
                (branch_id, agent_id, conversation, main, f"目标 {number}", f"request-{number}",
                 f"fingerprint-{number}", f"2026-10-18T00:00:0{number}Z",
                 f"2026-10-18T00:00:0{number}Z"),
            )
            db.execute(
                "INSERT INTO continuity_work_episodes(id,branch_id,ordinal,status,opened_at,source_epoch) "
                "VALUES(?,?,1,'paused',?,0)",
                (f"episode-{number}", branch_id, f"2026-10-18T00:00:0{number}Z"),
            )
            db.execute(
                "INSERT INTO continuity_episode_archives(archive_id,branch_id,episode_id,experience_id,"
                "outcome,archive_version,report,created_at) "
                "VALUES(?,?,?,?,'completed_unverified',1,'fixture',?)",
                (f"archive-{number}", branch_id, f"episode-{number}",
                 f"work-archive:list-{number}", f"2026-10-18T00:00:0{number}Z"),
            )
    statements = []
    original = app.database.connect

    def traced_connect():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(app.database, "connect", traced_connect)
    rows = continuity.list_work(agent_id)
    assert len(rows) == 8
    archive_reads = [statement for statement in statements
                     if "FROM continuity_episode_archives" in statement]
    assert len(archive_reads) == 1, archive_reads
    assert rows[-1]["latest_archive"]["archive_id"] == "archive-7"


def test_history_domain_defaults_full_and_pages_with_keyset(app, agent_id):
    continuity, branch, _ = _seed_branch(app, agent_id, 1, guidances=HISTORY_LIMIT + 3)
    _add_history(app, branch, HISTORY_LIMIT + 3)
    assert len(continuity.task_states(branch["id"])) == HISTORY_LIMIT + 4
    assert len(continuity.episodes(branch["id"])) == HISTORY_LIMIT + 4
    assert len(continuity.archives(branch["id"])) == HISTORY_LIMIT + 3
    assert len(continuity.guidance(branch["id"])) == HISTORY_LIMIT + 3

    current = continuity.task_states(branch["id"], limit=HISTORY_LIMIT)
    assert len(current) == HISTORY_LIMIT
    cursor = json.dumps({"kind": "task_state", "revision": current[0]["revision"]})
    older = continuity.task_states(branch["id"], limit=HISTORY_LIMIT, before=cursor)
    assert older[-1]["revision"] < current[0]["revision"]
    assert {row["revision"] for row in current}.isdisjoint(row["revision"] for row in older)

    episodes = continuity.episodes(branch["id"], limit=HISTORY_LIMIT)
    episode_cursor = json.dumps({"kind": "episode", "revision": episodes[0]["ordinal"]})
    previous = continuity.episodes(branch["id"], limit=HISTORY_LIMIT, before=episode_cursor)
    assert previous[-1]["ordinal"] < episodes[0]["ordinal"]

    archives = continuity.archives(branch["id"], limit=HISTORY_LIMIT)
    archive_cursor = json.dumps({"kind": "archive", "created_at": archives[0]["created_at"],
                                 "id": archives[0]["archive_id"]})
    previous_archives = continuity.archives(branch["id"], limit=HISTORY_LIMIT, before=archive_cursor)
    assert {row["archive_id"] for row in archives}.isdisjoint(
        row["archive_id"] for row in previous_archives)

    guides = continuity.guidance(branch["id"], limit=HISTORY_LIMIT)
    guide_cursor = json.dumps({"kind": "guidance", "created_at": guides[0]["created_at"],
                               "id": guides[0]["id"]})
    previous_guides = continuity.guidance(branch["id"], limit=HISTORY_LIMIT, before=guide_cursor)
    assert {row["id"] for row in guides}.isdisjoint(row["id"] for row in previous_guides)
    with pytest.raises(ValueError, match="游标"):
        continuity.guidance(branch["id"], limit=5, before="{bad")
    assert parse_history_cursor(json.dumps({"kind": "episode", "revision": 2}), "episode") == 2


def test_history_api_is_bounded_and_returns_array(client, headers):
    services = client.app.state.services
    agent_id = services.create_agent("分页验收")
    _, branch, _ = _seed_branch(services, agent_id, 1, guidances=103)
    base = f"/api/work-branches/{branch['id']}"
    page = client.get(base + "/guidance", headers=headers)
    assert page.status_code == 200
    first = page.json()
    assert isinstance(first, list) and len(first) == 100
    cursor = json.dumps({"kind": "guidance", "created_at": first[0]["created_at"],
                         "id": first[0]["id"]})
    older = client.get(base + "/guidance", params={"before": cursor}, headers=headers)
    assert older.status_code == 200
    assert isinstance(older.json(), list) and len(older.json()) == 3
    assert client.get(base + "/guidance", params={"limit": 101}, headers=headers).status_code == 422
    assert client.get(base + "/guidance", params={"before": "{bad"}, headers=headers).status_code == 422


def test_large_branch_list_and_history_windows(app, agent_id):
    """A local scale sample that catches cardinality growth, not a benchmark."""
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent_id, "主干")["id"]
    continuity.main.bind(agent_id, main, 0)
    with app.database.transaction() as db:
        for number in range(200):
            conversation = app.conversations.create(agent_id, f"规模 {number}")["id"]
            branch_id = f"scale-{number}"
            db.execute(
                "INSERT INTO continuity_work_branches(id,agent_id,conversation_id,main_conversation_id,"
                "objective,status,revision,request_id,fingerprint,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'paused',1,?,?,?,?)",
                (branch_id, agent_id, conversation, main, f"规模目标 {number}", f"request-{number}",
                 f"fingerprint-{number}", f"2026-10-18T00:{number // 60:02d}:{number % 60:02d}Z",
                 "2026-10-18T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO continuity_work_episodes(id,branch_id,ordinal,status,opened_at,source_epoch) "
                "VALUES(?,?,1,'paused',?,0)",
                (f"scale-episode-{number}", branch_id, "2026-10-18T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO continuity_episode_archives(archive_id,branch_id,episode_id,experience_id,"
                "outcome,archive_version,report,created_at) "
                "VALUES(?,?,?,?,'completed_unverified',1,'fixture',?)",
                (f"scale-archive-{number}", branch_id, f"scale-episode-{number}",
                 f"work-archive:scale-{number}", "2026-10-18T00:00:00Z"),
            )
        branch_id = "scale-0"
        for number in range(500):
            db.execute(
                "INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,"
                "progress,unfinished,verified,unknowns,artifacts,sources,grants,digest,created_at) "
                "VALUES(?,?,?,'[]','[]','[]','[]','[]','[]','[]','{}','fixture',?)",
                (branch_id, 1000 + number, "规模目标 0", f"2026-10-18T00:{number // 60:02d}:00Z"),
            )
    rows = continuity.list_work(agent_id)
    assert len(rows) == 200
    assert rows[-1]["latest_archive"]["archive_id"] == "scale-archive-199"
    page = continuity.task_states(branch_id, limit=100)
    assert len(page) == 100
    cursor = json.dumps({"kind": "task_state", "revision": page[0]["revision"]})
    older = continuity.task_states(branch_id, limit=100, before=cursor)
    assert len(older) == 100
    assert older[-1]["revision"] == page[0]["revision"] - 1


def test_task_state_patch_preserves_omitted_fields_and_clears_explicit_empty(app, agent_id):
    profile = app.profiles.save(
        "maintenance-profile",
        ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
                     base_url="https://fixture.invalid"),
        api_key="test-maintenance-key",
    )
    app.profiles.select(profile["id"], None)
    continuity, branch, _ = _seed_branch(app, agent_id, 1)

    async def scenario():
        from lingnexus.runtime import ChatRuntime

        runtime = ChatRuntime(app)
        try:
            run = runtime.submit(ChatRequest(
                agent_id=agent_id, conversation_id=branch["conversation_id"],
                message="maintenance", request_id="maintenance-state",
                work_episode_id=branch["current_episode_id"],
            ))
            state = continuity.work_domain.record_task_state(
                branch["id"], run["id"], decisions=["保留决策"], unfinished=["继续核对"],
                artifacts=["artifact.md"], sources=["source.md"],
            )
            updated = continuity.work_domain.record_task_state(
                branch["id"], run["id"], progress=["新进度"],
            )
            assert updated["decisions"] == state["decisions"]
            assert updated["unfinished"] == state["unfinished"]
            assert updated["artifacts"] == state["artifacts"]
            assert updated["sources"] == state["sources"]
            cleared = continuity.work_domain.record_task_state(
                branch["id"], run["id"], decisions=[],
            )
            assert cleared["decisions"] == []
            assert cleared["unfinished"] == state["unfinished"]
        finally:
            await runtime.aclose()

    import asyncio

    asyncio.run(scenario())


def test_task_state_merge_is_one_append_with_64_entry_ceiling(app, agent_id):
    profile = app.profiles.save(
        "ceiling-profile",
        ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
                     base_url="https://fixture.invalid"),
        api_key="test-ceiling-key",
    )
    app.profiles.select(profile["id"], None)
    continuity, branch, _ = _seed_branch(app, agent_id, 1)
    with app.database.transaction() as db:
        db.execute(
            "UPDATE continuity_work_branches SET status='running',task_state_revision=2 WHERE id=?",
            (branch["id"],),
        )
        db.execute(
            "UPDATE continuity_work_episodes SET task_state_revision=2 WHERE id=?",
            (branch["current_episode_id"],),
        )
        db.execute(
            "DELETE FROM continuity_work_task_states WHERE branch_id=?", (branch["id"],)
        )
        db.execute(
            "INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,"
            "progress,unfinished,verified,unknowns,artifacts,sources,grants,digest,created_at) "
            "VALUES(?,1,?,'[]',?,'[]','[]','[]','[]','[]','{}','fixture',?)",
            (branch["id"], branch["objective"], json.dumps([f"p{number}" for number in range(63)]),
             "2026-10-18T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,"
            "progress,unfinished,verified,unknowns,artifacts,sources,grants,digest,created_at) "
            "VALUES(?,2,?,'[]','[]','[]','[]','[]','[]','[]','{}','fixture',?)",
            (branch["id"], branch["objective"], "2026-10-18T00:00:01Z"),
        )
    with pytest.raises(ValueError, match="条目无效"):
        continuity.work_domain.record_task_state(
            branch["id"], "unused", progress=[f"p{number}" for number in range(65)]
        )
