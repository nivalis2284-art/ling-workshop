"""Smoke: the background consolidation path turns one message into memories."""

import asyncio
import json

import httpx

from lingnexus.models import ModelProfile

KEY = "fixture-learning-key-not-real"
OPAQUE = "opaque-private-reasoning-never-persist"


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


def add(app, agent_id, text="我希望回答简洁。茶会在周五举行。"):
    return app.history.add(agent_id, text, kind="user", request_id="source-1")


def result_document():
    return {
        "event": {"title": "茶会安排", "summary": "用户表示茶会在周五举行。"},
        "knowledge": [
            {"statement": "用户表示茶会在周五举行。", "kind": "fact", "subject": "world"}
        ],
        "changes": [],
    }


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


def reply(answer):
    def chunk(delta=None, finish=None):
        return {
            "id": "chat_fixture",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        }

    payload = "".join(
        "data: " + (item if isinstance(item, str) else json.dumps(item)) + "\n\n"
        for item in (
            chunk({"role": "assistant", "reasoning_content": "Visible summary"}),
            chunk({"content": answer, "opaque_signature": OPAQUE}),
            chunk(finish="stop"),
            {
                "id": "chat_fixture",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "native_private_field": OPAQUE,
                },
            },
            "[DONE]",
        )
    )
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=payload.encode()
    )


def test_learning_consolidates_once_and_keeps_memory_after_correction(app, agent_id):
    configure(app)
    ref = add(app, agent_id)
    assert add(app, agent_id) == ref
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert "tools" not in body
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert ref.id in request.content.decode()
        assert KEY not in request.content.decode()
        return reply(json.dumps(result_document()))

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
    assert json.loads(saved["usage"])["output_tokens"] == 4
    app.history.correct(ref, "茶会改为周六。")
    assert [item["id"] for item in app.memory.find_episodes(agent_id, "")] == [episode["id"]]
    assert [item["id"] for item in app.memory.knowledge.search(agent_id, "")] == [
        knowledge["id"]
    ]
