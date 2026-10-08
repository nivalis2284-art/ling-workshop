"""Real application/SQLite and native gateway; only external HTTP is mocked."""

import asyncio
import json

import httpx
import pytest
from test_chat_runtime import FAMILIES, success

from lingnexus.models import ModelProfile

KEY = "fixture-learning-key-not-real"


def configure(app, family="openai_chat_completions", key=KEY):
    profile = ModelProfile(
        api_family=family,
        model="fixture-learning",
        base_url="https://learning.invalid",
        max_output_tokens=2048,
    )
    item = app.profiles.save("Learning fixture", profile, api_key=key)
    app.profiles.select(item["id"], None)
    return item["id"]


def add(app, agent_id, text="我希望回答简洁。茶会在周五举行。", **kwargs):
    return app.history.add(
        agent_id,
        text,
        kind=kwargs.get("kind", "user"),
        scope=kwargs.get("scope", "real"),
        request_id=kwargs.get("request_id", "source-1"),
    )


def result_document(**overrides):
    return {
        "event": {"title": "茶会安排", "summary": "用户表示茶会在周五举行。"},
        "knowledge": [
            {"statement": "用户表示茶会在周五举行。", "kind": "fact", "subject": "world"}
        ],
        "changes": [],
    } | overrides


def response(document=None, family="openai_chat_completions", *, raw=None):
    answer = raw if raw is not None else json.dumps(document or result_document())
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=success(family, answer),
    )


def state(app, job_id):
    with app.database.transaction() as db:
        row = db.execute(
            "SELECT j.status,s.* FROM jobs j LEFT JOIN learning_job_state s "
            "ON j.id=s.job_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        return dict(row)


def counts(app):
    with app.database.transaction() as db:
        return [
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("episodes", "knowledge", "proposals")
        ]


def worker(app, client):
    from lingnexus.learning import ConsolidationWorker

    return ConsolidationWorker(app, client=client)


@pytest.mark.parametrize("family", FAMILIES)
def test_native_primary_persists_once_with_real_refs_and_usage(app, agent_id, family):
    configure(app, family)
    ref = add(app, agent_id)
    assert add(app, agent_id) == ref
    requests = []

    def handler(request):
        # An independent connection can write while the network call is active.
        db = app.database.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        finally:
            db.close()
        requests.append(request)
        body = json.loads(request.content)
        assert "tools" not in body
        if family == "anthropic_messages":
            assert body["system"] and body["max_tokens"] == 2048
            assert [m["role"] for m in body["messages"]] == ["user"]
            assert "input" not in body and "max_output_tokens" not in body
        elif family == "openai_responses":
            assert body["instructions"] and body["input"]
            assert body["max_output_tokens"] == 2048
            assert "messages" not in body and "max_tokens" not in body
        else:
            assert [m["role"] for m in body["messages"]] == ["system", "user"]
            assert body["max_completion_tokens"] == 2048
            assert "input" not in body and "max_tokens" not in body
        assert ref.id in request.content.decode()
        assert KEY not in request.content.decode()
        return response(family=family)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = worker(app, client)
            first = await service.run_once(agent_id)
            assert first["status"] == "done"
            assert (await service.drain_once(agent_id))["status"] == "idle"
            await service.aclose()
            assert not client.is_closed
            return first

    result = asyncio.run(scenario())
    assert len(requests) == 1
    assert counts(app) == [1, 1, 0]
    episode = app.memory.find_episodes(agent_id, "")[0]
    knowledge = app.memory.knowledge.search(agent_id, "")[0]
    assert episode["sources"] == knowledge["sources"] == [ref.model_dump()]
    saved = state(app, result["job_id"])
    assert saved["attempts"] == 1
    assert saved["outcome"] == "saved"
    assert saved["finished_at"]
    assert json.loads(saved["usage"])["output_tokens"] == 4
    app.history.correct(ref, "茶会改为周六。")
    assert app.memory.find_episodes(agent_id, "") == []
    assert app.memory.knowledge.search(agent_id, "") == []


def test_known_habits_auto_protected_changes_remain_candidates(app, agent_id):
    configure(app)
    add(app, agent_id)
    changes = [
        {"kind": "habit", "key": "communication.response_length", "text": "concise"},
        {"kind": "relationship", "key": "relationship.owner", "text": "我是用户的伴侣"},
        {"kind": "identity", "key": "identity.role", "text": "我是家庭成员"},
    ]

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: response(result_document(changes=changes))
            )
        ) as client:
            return await worker(app, client).run_once()

    assert asyncio.run(scenario())["status"] == "done"
    assert app.identity.current(agent_id).entries == {"communication.response_length": "concise"}
    proposals = app.identity.list_proposals(agent_id)
    assert {p["key"]: p["status"] for p in proposals} == {
        "communication.response_length": "applied_auto",
        "relationship.owner": "awaiting_owner",
        "identity.role": "awaiting_owner",
    }
    assert all(p["base_revision"] == 1 for p in proposals if p["status"] == "awaiting_owner")


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        json.dumps(
            result_document(
                knowledge=[{"statement": "我是用户的伴侣", "kind": "fact", "subject": "self"}]
            )
        ),
        json.dumps(
            result_document(
                knowledge=[
                    {
                        "statement": "Ling is now the owner's partner",
                        "kind": "fact",
                        "subject": "world",
                    }
                ]
            )
        ),
        json.dumps(
            result_document(changes=[{"kind": "habit", "key": "permissions.files", "text": "all"}])
        ),
        json.dumps(
            result_document(
                knowledge=[{"statement": "x" * 801, "kind": "fact", "subject": "world"}]
            )
        ),
        json.dumps(
            result_document(knowledge=[{"statement": "x", "kind": "fact", "subject": "world"}] * 9)
        ),
        json.dumps(
            result_document(
                knowledge=[{"statement": "Any ordinary text", "kind": "fact", "subject": "self"}]
            )
        ),
        json.dumps(result_document(knowledge=[{"statement": "Missing subject", "kind": "fact"}])),
        json.dumps(
            result_document(event={"title": "title", "summary": "summary", "sources": ["invented"]})
        ),
        '{"event":null,"knowledge":[],"changes":[],"changes":[]}',
    ],
)
def test_invalid_or_protected_knowledge_fails_without_partial_memory(app, agent_id, raw):
    configure(app)
    add(app, agent_id)
    calls = []

    def handler(request):
        calls.append(request)
        return response(raw=raw)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = worker(app, client)
            first = await service.run_once()
            assert (await service.run_once())["status"] == "idle"
            return first

    result = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert counts(app) == [0, 0, 0]
    assert len(calls) == 1
    assert state(app, result["job_id"])["error_code"] in {"invalid_response", "protected_knowledge"}


def test_correction_during_stream_discards_result_and_processes_new_revision(app, agent_id):
    configure(app)
    original = add(app, agent_id)
    revised = []

    class CorrectingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = success("openai_chat_completions", json.dumps(result_document()))
            yield data[: len(data) // 2]
            revised.append(app.history.correct(original, "茶会在周六举行"))
            yield data[len(data) // 2 :]

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=CorrectingStream()
                )
            )
        ) as client:
            stale = await worker(app, client).run_once()
        assert stale["status"] == "stale"
        assert counts(app) == [0, 0, 0]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: response(
                    result_document(
                        event={"title": "新安排", "summary": "用户表示茶会在周六举行"}, knowledge=[]
                    )
                )
            )
        ) as client:
            assert (await worker(app, client).run_once())["status"] == "done"

    asyncio.run(scenario())
    assert app.memory.find_episodes(agent_id, "")[0]["sources"] == [revised[0].model_dump()]


def test_missing_credentials_stays_queued_then_can_run(app, agent_id):
    configure(app, key=None)
    add(app, agent_id)
    calls = []

    def handler(request):
        calls.append(request)
        return response()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = worker(app, client)
            for _ in range(2):
                blocked = await service.run_once()
                assert blocked["status"] == "blocked_credentials"
            saved = state(app, blocked["job_id"])
            assert saved["status"] == "queued"
            assert saved["attempts"] == 0
            assert calls == []
            configure(app)
            assert (await service.run_once())["status"] == "done"

    asyncio.run(scenario())
    assert len(calls) == 1


def test_model_can_skip_and_source_filter_never_calls_model(app, agent_id):
    configure(app)
    add(app, agent_id, "hello")
    add(app, agent_id, kind="system", request_id="system")
    add(app, agent_id, scope="scene:fiction", request_id="scene")
    calls = []

    def handler(request):
        calls.append(request)
        return response({"event": None, "knowledge": [], "changes": []})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = worker(app, client)
            for _ in range(3):
                assert (await service.run_once())["status"] == "skipped"
            assert (await service.run_once())["status"] == "idle"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert counts(app) == [0, 0, 0]


def test_failure_is_redacted_and_only_explicit_retry_calls_again(app, agent_id, caplog):
    configure(app)
    add(app, agent_id)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError(KEY + " private raw exception")
        return response()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = worker(app, client)
            failed = await service.run_once()
            assert failed["status"] == "failed"
            assert (await service.run_once())["status"] == "idle"
            assert service.retry(failed["job_id"])["status"] == "queued"
            done = await service.run_once()
            assert done["status"] == "done"
            return done

    done = asyncio.run(scenario())
    assert len(calls) == 2
    assert state(app, done["job_id"])["attempts"] == 2
    with app.database.transaction() as db:
        dump = "\n".join(db.iterdump())
    assert KEY not in dump + caplog.text + repr(done)
    assert "private raw exception" not in dump + caplog.text + repr(done)


def test_competing_workers_cannot_claim_the_same_job(app, agent_id):
    configure(app)
    add(app, agent_id)
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = asyncio.create_task(worker(app, client).run_once(agent_id))
            await entered.wait()
            try:
                assert (await worker(app, client).run_once(agent_id))["status"] == "idle"
            finally:
                release.set()
            assert (await first)["status"] == "done"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert counts(app) == [1, 1, 0]


@pytest.mark.parametrize(
    "family,finish",
    [
        ("openai_chat_completions", "length"),
        ("anthropic_messages", "max_tokens"),
    ],
)
def test_valid_json_with_incomplete_finish_is_not_saved(app, agent_id, family, finish):
    configure(app, family)
    add(app, agent_id)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=success(family, json.dumps(result_document()), finish_reason=finish),
                )
            )
        ) as client:
            return await worker(app, client).run_once()

    result = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert result["error_code"] == "incomplete_response"
    assert counts(app) == [0, 0, 0]


def test_cancelled_stream_closes_and_does_not_automatically_replay(app, agent_id):
    configure(app)
    add(app, agent_id)
    job_id = app.history.pending_jobs(agent_id)[0]["id"]

    async def scenario():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=WaitingStream()
                )
            )
        ) as client:
            service = worker(app, client)
            task = asyncio.create_task(service.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            assert (await service.run_once())["status"] == "idle"

    asyncio.run(scenario())
    saved = state(app, job_id)
    assert saved["status"] == "failed"
    assert saved["outcome"] == saved["error_code"] == "interrupted"
    assert saved["attempts"] == 1
    assert counts(app) == [0, 0, 0]


@pytest.mark.parametrize("during_call", [False, True])
def test_deselected_source_is_stale_before_or_during_call(app, agent_id, during_call):
    configure(app)
    ref = add(app, agent_id)
    calls = []
    if not during_call:
        app.history.set_visible(ref, False)

    def handler(request):
        calls.append(request)
        app.history.set_visible(ref, False)
        return response()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await worker(app, client).run_once()

    result = asyncio.run(scenario())
    assert result["status"] == "stale"
    assert len(calls) == int(during_call)
    assert counts(app) == [0, 0, 0]


def test_self_change_during_call_does_not_overwrite_new_state(app, agent_id):
    configure(app)
    ref = add(app, agent_id)

    def handler(request):
        app.identity.propose(
            agent_id,
            kind="habit",
            key="communication.response_length",
            text="detailed",
            sources=[ref],
            base_revision=0,
            request_id="newer-state",
        )
        return response(
            result_document(
                changes=[
                    {"kind": "habit", "key": "communication.response_length", "text": "concise"}
                ]
            )
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await worker(app, client).run_once()

    result = asyncio.run(scenario())
    assert result["status"] == "stale"
    assert result["outcome"] == "self_changed"
    assert app.identity.current(agent_id).entries == {"communication.response_length": "detailed"}
    assert counts(app) == [0, 0, 1]


def test_job_done_write_failure_rolls_back_all_memory(app, agent_id):
    configure(app)
    add(app, agent_id)
    with app.database.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_completion BEFORE UPDATE OF status ON jobs "
            "WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response())
        ) as client:
            return await worker(app, client).run_once()

    result = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert counts(app) == [0, 0, 0]
    assert state(app, result["job_id"])["status"] == "failed"


def test_related_evidence_is_bounded_attached_and_revalidated(app, agent_id):
    configure(app)
    evidence = add(app, agent_id, "Orchard trees are watered weekly", request_id="evidence")
    app.memory.knowledge.save(
        agent_id,
        "Orchard trees are watered weekly",
        [evidence],
        kind="fact",
        request_id="knowledge",
    )
    current = add(app, agent_id, "Orchard has new trees", request_id="current")

    # Complete the evidence's queued job first without adding more memory.
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: response({"event": None, "knowledge": [], "changes": []})
            )
        ) as client:
            assert (await worker(app, client).run_once())["status"] == "skipped"

        def handler(request):
            content = json.loads(json.loads(request.content)["messages"][1]["content"])
            assert content["source"]["id"] == current.id
            assert 1 <= len(content["relevant_context"]) <= 4
            assert evidence.id in request.content.decode()
            app.history.correct(evidence, "Orchard needs daily watering")
            return response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await worker(app, client).run_once()
            assert result["status"] == "stale"
            with app.database.transaction() as db:
                row = db.execute(
                    "SELECT input_refs FROM learning_attempts WHERE job_id=?", (result["job_id"],)
                ).fetchone()
                assert {r["id"] for r in json.loads(row[0])} == {evidence.id, current.id}

    asyncio.run(scenario())
    assert counts(app) == [0, 1, 0]


def test_pending_candidate_cannot_be_laundered_as_world_knowledge(app, agent_id):
    configure(app)
    ref = add(app, agent_id)
    app.identity.propose(
        agent_id,
        kind="value",
        key="outlook",
        text="The moon is home",
        sources=[ref],
        base_revision=0,
        request_id="pending",
    )

    def handler(request):
        assert "The moon is home" not in request.content.decode()
        return response(
            result_document(
                knowledge=[{"statement": "The moon is home", "kind": "fact", "subject": "world"}]
            )
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await worker(app, client).run_once()

    result = asyncio.run(scenario())
    assert result["error_code"] == "protected_knowledge"
    assert counts(app) == [0, 0, 1]
    assert app.identity.current(agent_id).entries == {}


@pytest.mark.parametrize(
    "kind,prefix", [("assistant", "助手陈述"), ("tool", "工具记录"), ("observation", "观察记录")]
)
def test_allowed_sources_retain_reported_provenance(app, agent_id, kind, prefix):
    configure(app)
    add(app, agent_id, "Attempted a backup; it failed", kind=kind)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: response(
                    result_document(
                        knowledge=[
                            {
                                "statement": "The backup failed",
                                "kind": "experience",
                                "subject": "world",
                            }
                        ]
                    )
                )
            )
        ) as client:
            return await worker(app, client).run_once()

    assert asyncio.run(scenario())["status"] == "done"
    item = app.memory.knowledge.search(agent_id, "")[0]
    assert item["statement"].startswith(prefix)
    assert item["kind"] == "experience"


def test_filters_job_kind_agent_and_preserves_abandoned_running_claim(app, agent_id, app_factory):
    configure(app)
    ref = add(app, agent_id)
    job = app.history.pending_jobs(agent_id)[0]
    other = app.create_agent("Other")
    add(app, other)
    with app.database.transaction() as db:
        db.execute("UPDATE jobs SET kind='unrelated' WHERE id=?", (job["id"],))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response())
        ) as client:
            service = worker(app, client)
            assert (await service.run_once(agent_id))["status"] == "idle"
            assert (await service.run_once(other))["status"] == "done"
            with app.database.transaction() as db:
                db.execute(
                    "UPDATE jobs SET kind='consolidate',status='running' WHERE id=?", (job["id"],)
                )
            reopened = app_factory(app.database.path)
            try:
                assert (await worker(reopened, client).run_once(agent_id))["status"] == "idle"
            finally:
                reopened.close()

    asyncio.run(scenario())
    assert app.history.read(ref)
    assert counts(app) == [1, 1, 0]


def test_reflected_credentials_never_persist_in_valid_model_output(app, agent_id):
    configure(app)
    add(app, agent_id)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: response(result_document(event={"title": "Echo", "summary": KEY}))
            )
        ) as client:
            return await worker(app, client).run_once()

    result = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert result["error_code"] == "credential_in_response"
    with app.database.transaction() as db:
        assert KEY not in "\n".join(db.iterdump())
    assert counts(app) == [0, 0, 0]
