import asyncio
import json

import httpx
import pytest
from test_proactive_loop import loop as _proactive_loop_fixture
from test_proactive_loop import observations, response

from lingnexus.activities.investigation import ModelInvestigator
from lingnexus.models import APIFamily, ModelProfile

# Reuse the real-Application fixture within the two permitted test modules.
loop = _proactive_loop_fixture


def test_interest_review_uses_current_self_and_preserves_model_usage(app, agent_id):
    activity = app.activities.create(agent_id, kind="topic_review", title="跟进资料")
    event = app.activities.record_event(
        agent_id, dedupe_key="task:prepared", kind="task_completed", content="任务已完成"
    )
    profile = app.profiles.save(
        "主",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="test",
            base_url="https://example.test",
        ),
        api_key="fixture-interest-key",
    )
    app.profiles.select(profile["id"], None)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        decision = {
            "changed": True,
            "summary": "已有任务完成，可以继续核实。",
            "notification": "资料已准备好。",
        }
        frames = [
            {
                "id": "r",
                "choices": [
                    {"index": 0, "delta": {"content": json.dumps(decision)}, "finish_reason": None}
                ],
            },
            {
                "id": "r",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        ]
        body = (
            "".join("data: " + json.dumps(x) + chr(10) * 2 for x in frames)
            + "data: [DONE]"
            + chr(10) * 2
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            investigator = ModelInvestigator(app, client=client)
            decision = await investigator(
                activity,
                {"events": [event]},
            )
            assert decision["changed"] is True

    asyncio.run(run())
    assert "当前" in requests[0]["messages"][0]["content"]
    with app.database.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM activity_model_calls WHERE agent_id=?", (agent_id,)
            ).fetchone()[0]
            == 1
        )


def test_broad_directory_and_origin_grants_do_not_authorize_exploration(
    loop, app, agent_id, tmp_path
):
    (tmp_path / "private.txt").write_text("Do not discover this", encoding="utf-8")
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    app.activities.configure_web_origins(
        agent_id, ["https://updates.example.test"], expected_revision=0
    )
    loop.tick()
    assert loop.requests == [] and loop.web_requests == []
    assert observations(app) == [] and app.activities.notifications(agent_id) == []


@pytest.mark.parametrize("interval", [59, 0, True, 60.5, "60"])
def test_invalid_followup_interval_never_commits(loop, app, agent_id, interval):
    loop.watch()
    loop.decision["followup"] = {
        "kind": "topic_review",
        "title": "Parser compatibility",
        "interval_seconds": interval,
    }
    loop.tick()
    assert observations(app) == [] and app.activities.notifications(agent_id) == []
    assert not [a for a in app.activities.list(agent_id) if a["origin"] == "model"]


@pytest.mark.parametrize("target", ["sibling", "duplicate", "root"])
def test_file_followup_needs_exact_scope_and_never_duplicates(
    loop, app, agent_id, tmp_path, target
):
    watch = loop.watch("file_watch")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("Not authorized for watching", encoding="utf-8")
    targets = {"sibling": str(sibling), "duplicate": watch["target"], "root": str(tmp_path)}
    loop.decision["followup"] = {
        "kind": "file_watch",
        "title": "Follow parser",
        "target": targets[target],
        "interval_seconds": 60,
    }
    assert loop.tick()["outcome"] == "changed"
    assert not [a for a in app.activities.list(agent_id) if a["origin"] == "model"]
    assert len(app.activities.scopes(agent_id)) == 1


def test_file_permission_revocation_during_model_discards_observation(loop, app, agent_id):
    loop.watch("file_watch")
    loop.mutate = lambda: app.permissions.configure(
        agent_id, readable=[], writable=[], expected_revision=1
    )
    loop.tick()
    assert len(loop.requests) == 1
    assert observations(app) == [] and app.activities.notifications(agent_id) == []
    with app.database.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM settings WHERE key LIKE 'attention:%'").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("phase", ["source", "model"])
def test_revocation_while_http_stream_closes_prevents_results(loop, app, agent_id, phase):
    watch = loop.watch()
    loop.decision["followup"] = {
        "kind": "topic_review",
        "title": "New parser review",
        "interval_seconds": 60,
    }
    closed = []

    class ClosingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield loop.body if phase == "source" else response(loop.decision).content

        async def aclose(self):
            assert getattr(app.database._local, "connection", None) is None
            app.activities.revoke_scope(watch["scope_id"], expected_revision=1)
            closed.append(True)
            await asyncio.sleep(0)

    async def provider(request):
        if (request.method == "GET") == (phase == "source"):
            return httpx.Response(
                200,
                stream=ClosingStream(),
                headers={
                    "Content-Type": "text/plain" if phase == "source" else "text/event-stream"
                },
            )
        return await loop.provider(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            app.activities.investigate = ModelInvestigator(app, client=client)
            await app.activities.tick("2026-10-09T12:00:00+00:00")

    asyncio.run(run())
    assert closed == [True]
    assert observations(app) == [] and app.activities.notifications(agent_id) == []
    assert not [a for a in app.activities.list(agent_id) if a["origin"] == "model"]


def test_same_followup_after_new_fact_still_creates_only_one(loop, app, agent_id):
    loop.watch()
    loop.decision["followup"] = {
        "kind": "topic_review",
        "title": "Parser tests",
        "interval_seconds": 3600,
    }
    loop.tick()
    loop.body = b"Compiler 2.1 release contains additional parser fixes."
    loop.tick(60)
    assert len(observations(app)) == 2
    created = [a for a in app.activities.list(agent_id) if a["origin"] == "model"]
    assert len(created) == 1 and created[0]["title"] == "Parser tests"


def test_interest_with_no_evidence_does_not_invent_external_changes(app, agent_id):
    activity = app.activities.create(agent_id, kind="topic_review", title="跟进")

    async def run():
        investigator = ModelInvestigator(app)
        try:
            assert await investigator(activity, {"events": []}) is None
        finally:
            await investigator.aclose()

    asyncio.run(run())


def test_escaped_credential_in_model_text_cannot_be_persisted(loop, app, agent_id):
    loop.watch()
    loop.decision["notification"] = "fixture-attention-key"

    async def provider(request):
        if request.method == "GET":
            return await loop.provider(request)
        raw = json.dumps(loop.decision).replace(
            "fixture-attention-key", r"\u0066ixture-attention-key"
        )
        frames = [
            {"id": "r", "choices": [{"index": 0, "delta": {"content": raw}}]},
            {"id": "r", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(f) + "\n\n" for f in frames)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=body + "data: [DONE]\n\n"
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            app.activities.investigate = ModelInvestigator(app, client=client)
            await app.activities.tick("2026-10-09T12:00:00+00:00")

    asyncio.run(run())
    assert observations(app) == [] and app.activities.notifications(agent_id) == []


def test_followup_duplicate_detection_reads_beyond_first_activity_page(loop, app, agent_id):
    loop.watch()
    for i in range(100):
        app.activities.create(
            agent_id, kind="reminder", title=f"Later {i}", due_at="2027-10-09T12:00:00+00:00"
        )
    app.activities.create(
        agent_id, kind="topic_review", title="Parser tests", due_at="2027-10-09T12:00:00+00:00"
    )
    loop.decision["followup"] = {
        "kind": "topic_review",
        "title": "parser tests",
        "interval_seconds": 60,
    }
    assert loop.tick()["outcome"] == "changed"
    assert not [a for a in app.activities.list(agent_id, offset=100) if a["origin"] == "model"]
