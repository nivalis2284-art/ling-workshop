"""Owner-maintained smoke layer: the survival paths that must never break.

The historical 1300+ test suite was retired on 2026-10-10 because a full run
cost ~16 minutes. This file is the acceptance tool now: keep it fast (tens of
seconds), keep it honest, and keep every assertion connected to a real failure
mode. Deeper one-off checks can be written ad hoc when a change needs them.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict, SourceInvalid
from lingnexus.common.types import SourceRef, new_id, utc_now
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime.payload import build_payload

# --- fixtures -------------------------------------------------------------


@pytest.fixture
def maintenance(app):
    from lingnexus.memory.maintenance import MemoryMaintenance

    return MemoryMaintenance(app)


@pytest.fixture
def client(tmp_path):
    from lingnexus.api.app import create_app

    server = create_app(
        data_dir=tmp_path, token="test-only-local-token", allowed_origins={"http://localhost:8765"}
    )
    with TestClient(server, base_url="http://localhost:8765") as value:
        yield value


@pytest.fixture
def agent(client):
    return client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]


def headers():
    return {"Authorization": "Bearer test-only-local-token"}


def add_entry(
    client, agent, *, kind="identity", key="identity.description", text="喜欢先问清楚", rev=0
):
    return client.post(
        f"/agents/{agent}/entries",
        headers=headers(),
        json={
            "kind": kind,
            "key": key,
            "text": text,
            "expected_revision": rev,
            "request_id": f"add-{key}-{rev}",
        },
    )


# --- shared builders ------------------------------------------------------


def source(app, agent_id, text="观察依据"):
    return app.history.add(agent_id, text, kind="user", request_id=text)


def entry(app, agent_id, kind="episodes", text="原始摘要"):
    ref = app.history.add(agent_id, "保留的原始记录", kind="user", request_id=new_id())
    if kind == "episodes":
        item_id = app.memory.save_episode(agent_id, "原始标题", text, [ref], request_id=new_id())
    else:
        item_id = new_id()
        with app.database.transaction() as db:
            db.execute(
                "INSERT INTO knowledge "
                "(id,agent_id,statement,kind,scope,status,request_id,fingerprint,created_at) "
                "VALUES (?,?,?,'fact',?,'active',?,'seed',?)",
                (item_id, agent_id, text, "", new_id(), utc_now()),
            )
            db.execute("INSERT INTO knowledge_sources VALUES (?,?,?)", (item_id, ref.id, 1))
    return item_id, ref


def proposal(
    app, agent_id, *, kind="identity", key="identity.description", text="喜欢探索", source=None
):
    ref = source or app.history.add(agent_id, text, kind="user", request_id=text)
    return app.identity.propose(
        agent_id,
        kind=kind,
        key=key,
        text=text,
        sources=[ref],
        base_revision=app.identity.current(agent_id).revision,
        request_id="p-" + text,
    )


def profile():
    return ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="test", base_url="https://example.test"
    )


# --- persistence and audit -----------------------------------------------


def test_reopen_preserves_identity_and_history(app_factory, tmp_path: Path):
    first = app_factory(tmp_path / "ling.db")
    agent_id = first.create_agent("灵")
    ref = first.history.add(
        agent_id, "这是测试经历", kind="user", request_id="test-request-1"
    )
    first.close()
    second = app_factory(tmp_path / "ling.db")
    assert second.identity.current(agent_id).agent_id == agent_id
    assert second.memory.read_sources([ref])[0]["text"] == "这是测试经历"
    second.close()


def test_duplicate_request_has_one_record(app, agent_id):
    kwargs = {"kind": "user", "request_id": "test-request-2"}
    assert app.history.add(agent_id, "内容", **kwargs) == app.history.add(agent_id, "内容", **kwargs)
    assert app.history.count(agent_id) == 1


def test_duplicate_request_with_changed_content_is_rejected(app, agent_id):
    args = {"kind": "user", "request_id": "same"}
    app.history.add(agent_id, "原文", **args)
    with pytest.raises(Conflict):
        app.history.add(agent_id, "变化", **args)
    assert app.history.count(agent_id) == 1


def test_transaction_rolls_back_record_and_job(app, agent_id):
    with pytest.raises(RuntimeError), app.database.transaction():
        app.history.add(agent_id, "应当回滚", kind="user", request_id="rollback")
        raise RuntimeError("simulated failed operation")
    assert app.history.count(agent_id) == 0
    assert app.history.pending_jobs(agent_id) == []


def test_export_restores_knowledge_and_goal_history(app, agent_id, tmp_path, app_factory):
    ref = source(app, agent_id)
    kid = app.memory.knowledge.save(agent_id, "知识", [ref], kind="fact", request_id="k")
    goal = app.development.propose_goal(agent_id, "目标", [ref], request_id="g")
    out = tmp_path / "export"
    app.maintenance.export_to(out)
    app.maintenance.restore_from(out, tmp_path / "restored.db")
    other = app_factory(tmp_path / "restored.db")
    assert other.memory.knowledge.read(kid)["statement"] == "知识"
    assert other.development.read(goal["id"])["status"] == "proposed"
    other.close()


# --- context and recall ---------------------------------------------------


def test_long_history_is_bounded_and_newest_message_tail(app, agent_id):
    refs = [
        app.history.add(
            agent_id,
            "旧交流" + str(i) + ":" + "重复过程" * 90,
            kind="user" if i % 2 == 0 else "assistant",
            request_id=str(i),
        )
        for i in range(20)
    ]
    newest = app.history.add(agent_id, "继续当前问题", kind="user", request_id="latest")
    prof = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="test",
        base_url="https://example.test",
        context_window_tokens=8000,
        max_output_tokens=1000,
    )
    payload, manifest = build_payload(app, agent_id, prof, [*refs, newest])
    assert payload["messages"][-1]["content"] == "继续当前问题"
    assert manifest["omitted"]
    assert len(str(payload).encode()) < 8000
    assert app.history.read(refs[0])["text"].startswith("旧交流0:")


def test_relevant_old_memory_is_recalled_with_precise_source(app, agent_id):
    old = app.history.add(
        agent_id, "复查约定：周五15:30，地点东楼。", kind="user", request_id="old"
    )
    app.memory.save_episode(
        agent_id, "复查约定", "约定周五复查，时间和地点见原文", [old], request_id="old-episode"
    )
    current = app.history.add(
        agent_id, "复查约定的地点是什么？", kind="user", request_id="query"
    )
    payload, manifest = build_payload(
        app,
        agent_id,
        ModelProfile(
            api_family=APIFamily.OPENAI_RESPONSES, model="test", base_url="https://example.test"
        ),
        [current],
    )
    assert "东楼" in str(payload)
    assert old.model_dump() in manifest["recall_sources"]


def test_deselected_branch_memory_does_not_return_to_context(app, agent_id):
    old = app.history.add(agent_id, "咖啡偏好", kind="user", request_id="coffee")
    app.memory.save_episode(agent_id, "咖啡偏好", "喜欢咖啡", [old], request_id="memory")
    app.history.set_visible(old, False)
    current = app.history.add(agent_id, "咖啡偏好？", kind="user", request_id="query")
    payload, manifest = build_payload(app, agent_id, profile(), [current])
    assert "喜欢咖啡" not in str(payload)
    assert manifest["recall_sources"] == []


def test_recall_deduplicates_same_source_and_covers_multiple_events(app, agent_id):
    from lingnexus.context.recall import candidates

    a = app.history.add(agent_id, "楠桥项目预算1760，负责人顾宁", kind="user", request_id="a")
    b = app.history.add(agent_id, "溪站项目预算1280，负责人韩川", kind="user", request_id="b")
    app.memory.save_episode(agent_id, "楠桥项目", "预算1760", [a], request_id="a-e")
    app.memory.knowledge.save(agent_id, "楠桥项目预算1760", [a], kind="fact", request_id="a-k")
    app.memory.save_episode(agent_id, "楠桥迭代", "负责人顾宁", [a], request_id="a-e2")
    app.memory.save_episode(agent_id, "溪站项目", "预算1280", [b], request_id="b-e")
    text, refs, markers = candidates(app, agent_id, "楠桥 溪站：分别预算多少？")
    assert "1760" in text and "1280" in text
    assert {r.id for r in refs} == {a.id, b.id}
    assert len(markers) == 2


# --- memory upkeep (owner correction first) -------------------------------


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("episodes", {"title": "主人更正标题", "summary": "主人更正摘要"}),
        ("knowledge", {"statement": "主人更正主张"}),
    ],
)
def test_correction_is_versioned_owner_assertion_with_original_refs(
    app, agent_id, maintenance, kind, changes, app_factory
):
    item_id, ref = entry(app, agent_id, kind)
    updated = maintenance.correct(kind, item_id, expected_revision=1, **changes)
    assert updated["revision"] == 2
    assert updated["provenance"] == "owner_assertion"
    assert updated["sources"] == [ref.model_dump()]
    assert all(updated[key] == value for key, value in changes.items())
    assert app.history.read(ref)["text"] == "保留的原始记录"
    with pytest.raises(RevisionConflict):
        maintenance.correct(kind, item_id, expected_revision=1, **changes)


def test_episode_fts_replaces_old_content(app, agent_id, maintenance):
    item_id, _ = entry(app, agent_id, text="obsoleteword")
    maintenance.correct("episodes", item_id, expected_revision=1, summary="replacementword")
    assert app.memory.find_episodes(agent_id, "obsoleteword") == []
    assert app.memory.find_episodes(agent_id, "replacementword")[0]["id"] == item_id


@pytest.mark.parametrize("kind", ["episodes", "knowledge"])
def test_archive_restore_delete_preserves_raw_history_and_audit(
    app, agent_id, maintenance, kind
):
    item_id, ref = entry(app, agent_id, kind)
    archived = maintenance.set_state(kind, item_id, action="archive", expected_revision=1)
    assert (archived["status"], archived["revision"]) == ("archived", 2)
    assert maintenance.items(agent_id, kind=kind, status="active") == []
    assert maintenance.items(agent_id, kind=kind, status="archived")[0]["id"] == item_id
    restored = maintenance.set_state(kind, item_id, action="restore", expected_revision=2)
    assert (restored["status"], restored["revision"]) == ("active", 3)
    deleted = maintenance.set_state(kind, item_id, action="delete", expected_revision=3)
    assert (deleted["status"], deleted["revision"]) == ("deleted", 4)
    with pytest.raises(NotFound):
        maintenance.read(kind, item_id)
    assert app.history.read(ref)["text"] == "保留的原始记录"
    with app.database.transaction() as db:
        singular = "episode" if kind == "episodes" else "knowledge"
        assert (
            db.execute(
                f"SELECT count(*) FROM {singular}_sources WHERE {singular}_id=?", (item_id,)
            ).fetchone()[0]
            == 0
        )
        edits = db.execute(
            "SELECT * FROM memory_edits WHERE target_id=? ORDER BY revision", (item_id,)
        ).fetchall()
        assert [row["action"] for row in edits] == ["archive", "restore", "delete"]


@pytest.mark.parametrize("kind", ["episodes", "knowledge"])
def test_legacy_invalidated_memory_accepts_owner_correction_and_restore(
    app, agent_id, maintenance, kind
):
    item_id, _ = entry(app, agent_id, kind)
    with app.database.transaction() as db:
        db.execute(f"UPDATE {kind} SET status='invalidated' WHERE id=?", (item_id,))
    presented = maintenance.read(kind, item_id)
    assert presented["source_valid"] is False
    assert "作废" in presented["source_notice"]
    corrected = maintenance.correct(
        kind,
        item_id,
        expected_revision=1,
        **({"summary": "人工修正"} if kind == "episodes" else {"statement": "人工修正"}),
    )
    assert corrected["revision"] == 2
    restored = maintenance.set_state(kind, item_id, action="restore", expected_revision=2)
    assert restored["status"] == "active"


def test_marker_checks_agent_source_and_status(app, agent_id, maintenance):
    item_id, ref = entry(app, agent_id)
    marker = maintenance.snapshot_marker("episodes", item_id)
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(app.create_agent("其他个体"), [marker])
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(agent_id, [marker | {"revision": True}])
    app.history.set_visible(ref, False)
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(agent_id, [marker])


# --- cascade retirement: derived memory survives --------------------------


def test_edit_user_forks_and_keeps_derived_memory(app, agent_id):
    convo = app.conversations.create(agent_id, "日常交流")
    user = app.conversations.append_user(convo["id"], "喜欢咖啡", request_id="coffee")
    answer = app.conversations.append_assistant(
        convo["id"], "记住咖啡", request_id="answer", expected_head=user["id"]
    )
    memory = app.memory.knowledge.save(
        agent_id, "喜欢咖啡", [user["source"]], kind="preference", request_id="pref"
    )
    edited = app.conversations.edit_user(convo["id"], user["id"], "喜欢茶", request_id="tea")
    assert [row["text"] for row in app.conversations.messages(convo["id"])] == ["喜欢茶"]
    with pytest.raises(SourceInvalid):
        app.history.read(answer["source"])
    assert app.memory.knowledge.read(memory)["statement"] == "喜欢咖啡"
    assert app.history.read(user["source"], active_only=False)["text"] == "喜欢咖啡"
    assert edited["id"] != user["id"]


def test_delete_turn_removes_round_and_keeps_derived_memory(app, agent_id):
    convo = app.conversations.create(agent_id, "日常交流")
    _, first_reply = _turn(app, convo["id"], "你好", "u1")
    second, _ = _turn(app, convo["id"], "喜欢咖啡吗", "u2")
    memory = app.memory.knowledge.save(
        agent_id, "用户喜欢咖啡", [second["source"]], kind="preference", request_id="pref"
    )
    app.conversations.delete_turns(convo["id"], [second["id"]])
    assert [row["text"] for row in app.conversations.messages(convo["id"])] == [
        "你好",
        "回复你好",
    ]
    with pytest.raises(SourceInvalid):
        app.history.read(second["source"])
    assert app.memory.knowledge.read(memory)["statement"] == "用户喜欢咖啡"
    assert app.history.read(first_reply["source"])["text"] == "回复你好"


def _turn(app, convo_id, text, request_id):
    user = app.conversations.append_user(convo_id, text, request_id=request_id)
    answer = app.conversations.append_assistant(
        convo_id, "回复" + text, request_id=request_id + "-a", expected_head=user["id"]
    )
    return user, answer


def test_knowledge_keeps_fact_after_source_correction(app, agent_id):
    ref = source(app, agent_id)
    item = app.memory.knowledge.save(
        agent_id, "偏好先看概览", [ref], kind="preference", request_id="fact"
    )
    assert app.memory.knowledge.search(agent_id, "概览")[0]["id"] == item
    app.history.correct(ref, "新偏好还未确认")
    assert app.memory.knowledge.search(agent_id, "概览")[0]["id"] == item
    assert app.memory.knowledge.read(item)["statement"] == "偏好先看概览"


def test_episode_summary_survives_source_correction(app, agent_id):
    ref = app.history.add(
        agent_id, "周五15:30去复查", kind="user", request_id="appointment"
    )
    episode = app.memory.save_episode(agent_id, "复查", "约定了复查", [ref], request_id="episode")
    assert "15:30" in app.memory.read_sources([ref])[0]["text"]
    app.history.correct(ref, "改到下周")
    assert app.memory.read_episode(episode)["summary"] == "约定了复查"
    assert [row["id"] for row in app.memory.find_episodes(agent_id, "复查")] == [episode]


# --- persona evolution ----------------------------------------------------


def test_protected_state_waits_for_owner_and_survives_restart(app, agent_id, app_factory):
    item = proposal(app, agent_id)
    assert item["status"] == "awaiting_owner"
    assert app.identity.current(agent_id).entries == {}
    app.identity.decide(item["id"], "approve", expected_revision=0)
    reopened = app_factory(app.database.path)
    assert reopened.identity.current(agent_id).entries == {"identity.description": "喜欢探索"}
    assert reopened.identity.current(agent_id).revision == 1
    reopened.close()


def test_low_risk_structured_preference_can_apply_automatically(app, agent_id):
    item = proposal(
        app, agent_id, kind="habit", key="communication.response_length", text="concise"
    )
    assert item["status"] == "applied_auto"
    assert app.identity.current(agent_id).entries["communication.response_length"] == "concise"


def test_pending_proposal_can_apply_after_source_correction(app, agent_id):
    item = proposal(app, agent_id)
    app.history.correct(SourceRef(**item["sources"][0]), "已经纠正")
    decided = app.identity.decide(item["id"], "approve", expected_revision=0)
    assert decided["status"] == "applied_owner"
    assert app.identity.current(agent_id).entries == {"identity.description": "喜欢探索"}


# --- skills ---------------------------------------------------------------


def test_skill_candidate_cannot_be_loaded_as_effective_until_owner_activates(app, agent_id):
    ref = app.history.add(agent_id, "任务过程记录", kind="tool", request_id="task")
    item = app.skills.save(
        agent_id, "复查资料", "核对来源的办法", "先查询原文，再核对日期。", [ref], request_id="skill"
    )
    assert item["status"] == "candidate"
    assert app.skills.available(agent_id) == []
    app.skills.decide(item["id"], "activate", expected_revision=1)
    assert app.skills.available(agent_id)[0]["name"] == "复查资料"
    assert app.skills.read(item["id"])["instructions"].startswith("先查询原文")


# --- task runtime ---------------------------------------------------------


def chat(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {"index": i, "id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (cid, name, args) in enumerate(calls)
        ]
    frame = {"id": "chat-native", "choices": [{"index": 0, "delta": delta,
              "finish_reason": "tool_calls" if calls else "stop"}]}
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def configure(app, family="openai_chat_completions"):
    prof = ModelProfile(api_family=family, model="fixture", base_url="https://fixture.test")
    pid = app.profiles.save("fixture", prof, api_key="runtime-secret-only")["id"]
    app.profiles.select(pid, None)


def client_for(responses, requests):
    def handler(request):
        requests.append(json.loads(request.content))
        value = responses.pop(0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=value)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def settle(runtime, tid):
    for _ in range(1500):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            return runtime.get(tid)
        await asyncio.sleep(.01)
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
            chat("Plan: write then check.",
                 [("write-id", "create_file", {"path": str(target), "text": "actual artifact"})]),
            chat(calls=[("check-id", "read_file", {"path": str(target)})]),
            chat("Delivered and checked answer.txt."),
        ]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            spec = {"agent_id": agent_id, "objective": "Create and verify answer.txt",
                    "request_id": "durable-one"}
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
                e["type"] for e in events
            }
            with app.database.transaction() as db:
                for table in TASK_TABLES:
                    rows = db.execute("SELECT * FROM " + table).fetchall()
                    assert "runtime-secret-only" not in repr([tuple(r) for r in rows])
            await runtime.close()

    asyncio.run(run())


def test_correcting_uncaptured_source_while_model_runs_does_not_block_task(app, agent_id):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        ref = app.history.add(agent_id, "identity evidence", kind="user",
                              request_id="self-evidence")
        app.identity.propose(agent_id, kind="identity", key="identity.name", text="Pinned name",
                             sources=[ref], base_revision=0, request_id="self-proposal")
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=chat("Answered without stale plans."))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id,
                                        "objective": "answer using current self",
                                        "request_id": "uncaptured"})
            await asyncio.wait_for(entered.wait(), 2)
            app.history.correct(ref, "withdraw identity evidence")
            release.set()
            state = await settle(runtime, tid)
            assert state["status"] == "needs_review", state
            assert state["error_code"] == "outcome_needs_review"
            await runtime.close()

    asyncio.run(run())


# --- api boundary and security -------------------------------------------


def test_local_api_denies_missing_token_and_foreign_origin(client):
    assert client.get("/health").status_code == 200
    assert client.get("/agents").status_code == 401
    assert (
        client.get(
            "/agents", headers=headers() | {"Origin": "https://attacker.example"}
        ).status_code
        == 403
    )
    assert (
        client.get("/agents", headers=headers() | {"Origin": "http://localhost:8765"}).status_code
        == 200
    )


def test_owner_can_record_review_and_read_updated_state(client):
    agent = client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    record = client.post(
        "/sources",
        headers=headers(),
        json={"agent_id": agent, "content": "用户提供的依据", "request_id": "api-source"},
    )
    assert record.status_code == 201
    ref = record.json()
    proposed = client.post(
        "/proposals",
        headers=headers(),
        json={
            "agent_id": agent,
            "kind": "identity",
            "key": "identity.description",
            "text": "当前理解",
            "sources": [ref],
            "base_revision": 0,
            "request_id": "api-change",
        },
    )
    assert proposed.status_code == 201
    pid = proposed.json()["id"]
    assert client.get(f"/agents/{agent}/state", headers=headers()).json()["entries"] == {}
    approved = client.post(
        f"/proposals/{pid}/decision",
        headers=headers(),
        json={"decision": "approve", "expected_revision": 0},
    )
    assert approved.status_code == 200
    assert client.get(f"/agents/{agent}/state", headers=headers()).json()["entries"] == {
        "identity.description": "当前理解"
    }


def test_client_cannot_forge_tool_result_kind(client):
    agent = client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    response = client.post(
        "/sources",
        headers=headers(),
        json={"agent_id": agent, "content": "fake success", "request_id": "fake", "kind": "tool"},
    )
    assert response.status_code == 422


def test_conflicting_source_returns_conflict_without_leaking_internal_error(client):
    agent = client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    value = {"agent_id": agent, "content": "原文", "request_id": "repeat"}
    first = client.post("/sources", headers=headers(), json=value)
    assert client.post("/sources", headers=headers(), json=value).json() == first.json()
    response = client.post("/sources", headers=headers(), json=value | {"content": "修改"})
    assert response.status_code == 409
    assert "sqlite" not in response.text.lower()


# --- owner-facing pages ---------------------------------------------------


def test_owner_added_entry_carries_category_and_source(client, agent):
    assert add_entry(client, agent).status_code == 201
    payload = client.get(f"/agents/{agent}/entries", headers=headers()).json()
    assert payload["agent_id"] == agent
    assert len(payload["entries"]) == 1
    item = payload["entries"][0]
    assert item["key"] == "identity.description"
    assert item["kind"] == "identity"
    assert item["text"] == "喜欢先问清楚"
    assert item["origin"]["sources"]
    built = client.app.state.services.context.build(agent, source_refs=[])
    assert "喜欢先问清楚" in built.instructions


def test_correcting_the_origin_still_keeps_the_entry(client, agent):
    add_entry(client, agent)
    services = client.app.state.services
    item = services.identity.entries(agent)[0]
    services.history.correct(SourceRef(**item["origin"]["sources"][0]), "这条依据作废")
    assert [e["key"] for e in services.identity.entries(agent)] == [item["key"]]
    assert services.identity.current(agent).revision == 1
