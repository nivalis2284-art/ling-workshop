"""Real application/state boundaries, with synthetic HTTP and DNS only."""

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lingnexus.activities.investigation import ModelInvestigator
from lingnexus.models import APIFamily, ModelProfile

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
ORIGIN = "https://updates.example.test"
URL = ORIGIN + "/releases"


def response(decision):
    frames = [
        {"id": "review", "choices": [{"index": 0, "delta": {"content": json.dumps(decision)}}]},
        {
            "id": "review",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "private": "fixture-attention-key",
            },
        },
    ]
    body = "".join("data: " + json.dumps(f) + "\n\n" for f in frames)
    return httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, content=body + "data: [DONE]\n\n"
    )


@pytest.fixture
def loop(app, agent_id, tmp_path, monkeypatch):
    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    profile = app.profiles.save(
        "Main",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="test",
            base_url="https://model.example.test",
        ),
        api_key="fixture-attention-key",
    )
    app.profiles.select(profile["id"], None)

    class Loop:
        def __init__(self):
            self.body = b"Compiler 2.0 released; fixes parser crashes."
            self.decision = {
                "changed": True,
                "summary": "Compiler update fixes parser crashes.",
                "notification": "Compiler 2.0 is available for your parser work.",
            }
            self.requests, self.web_requests = [], []
            self.mutate, self.phase = None, "model"
            self.item = app.activities.create(
                agent_id, kind="topic_review", title="Compiler progress", due_at=NOW
            )

        def watch(self, kind="web_watch", name="release.txt"):
            if kind == "web_watch":
                if not app.activities.web_origins(agent_id)["revision"]:
                    app.activities.configure_web_origins(agent_id, [ORIGIN], expected_revision=0)
                target = URL if name == "release.txt" else ORIGIN + "/" + name
            else:
                path = tmp_path / name
                path.write_text(self.body.decode(), encoding="utf-8")
                if not app.permissions.current(agent_id)["revision"]:
                    app.permissions.configure(
                        agent_id, readable=[tmp_path], writable=[], expected_revision=0
                    )
                target = str(path)
            return app.activities.create(
                agent_id,
                kind=kind,
                title="Compiler source",
                target=target,
                due_at=NOW + timedelta(days=365),
            )

        async def provider(self, request):
            assert getattr(app.database._local, "connection", None) is None
            if request.method == "GET":
                self.web_requests.append(request)
                phase = "source"
            else:
                self.requests.append(json.loads(request.content))
                phase = "model"
            if self.mutate and phase == self.phase:
                self.mutate()
                await asyncio.sleep(0)
            return (
                httpx.Response(200, content=self.body)
                if phase == "source"
                else response(self.decision)
            )

        def tick(self, seconds=0):
            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(self.provider)
                ) as client:
                    app.activities.investigate = ModelInvestigator(app, client=client)
                    return await app.activities.tick(NOW + timedelta(seconds=seconds))

            return asyncio.run(run())

        def data(self):
            return json.loads(self.requests[-1]["messages"][-1]["content"])

    return Loop()


def observations(app):
    with app.database.transaction() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT s.scope,r.content FROM sources s JOIN source_revisions r ON r.source_id=s.id "
                "WHERE s.kind='observation' AND s.request_id LIKE 'attention-observation:%'"
            )
        ]


@pytest.mark.parametrize("kind,scope", [("web_watch", "external"), ("file_watch", "real")])
def test_empty_event_review_observes_exact_authorized_fact(loop, app, agent_id, kind, scope):
    watch = loop.watch(kind)
    assert loop.tick()["outcome"] == "changed"
    data = loop.data()
    assert data["evidence"]["events"] == []
    observed = data["evidence"]["observations"][0]
    assert observed["target"] == watch["target"]
    assert "fixes parser crashes" in observed["excerpt"]
    facts = observations(app)
    assert len(facts) == 1 and facts[0]["scope"] == scope
    assert "fixes parser crashes" in facts[0]["content"]
    assert app.activities.notifications(agent_id)[0]["message"] == loop.decision["notification"]
    with app.database.transaction() as db:
        call = db.execute("SELECT usage,status FROM activity_model_calls").fetchone()
        assert call["status"] == "completed"
        assert json.loads(call["usage"])["total_tokens"] == 30
        assert "private" not in call["usage"] and "fixture-attention-key" not in call["usage"]


def test_hash_survives_restart_unchanged_quiet_and_new_fact_reacts(
    loop, app, agent_id, app_factory
):
    loop.watch()
    loop.tick()
    count = app.history.count(agent_id)
    reopened = app_factory(app.database.path)

    async def unchanged():
        async with httpx.AsyncClient(transport=httpx.MockTransport(loop.provider)) as client:
            decision = await ModelInvestigator(reopened, client=client)(
                reopened.activities.read(loop.item["id"]), {"events": []}
            )
            assert decision["changed"] is False and decision["notification"] is None

    try:
        asyncio.run(unchanged())
    finally:
        reopened.close()
    assert len(loop.requests) == 1 and app.history.count(agent_id) == count
    loop.body = b"Compiler 2.1 released; supports incremental parsing."
    loop.decision = {
        "changed": True,
        "summary": "Incremental parsing is now supported.",
        "notification": "Try incremental parsing in your compiler project.",
    }
    assert loop.tick(60)["outcome"] == "changed"
    assert len(observations(app)) == 2 and len(app.activities.notifications(agent_id)) == 2


def test_observation_budget_is_two_and_directory_or_origin_is_not_an_exact_grant(
    loop, app, agent_id
):
    for name in ("first", "second", "third"):
        loop.watch(name=name)
    loop.watch("file_watch")
    assert loop.tick()["outcome"] == "changed"
    assert len(loop.data()["evidence"]["observations"]) == 2
    assert len(observations(app)) == 2 and len(loop.web_requests) == 2
    assert {o["target"] for o in loop.data()["evidence"]["observations"]} <= {
        s["target"] for s in app.activities.scopes(agent_id)
    }


@pytest.mark.parametrize("phase", ["source", "model"])
@pytest.mark.parametrize(
    "change", ["scope", "origin", "permissions", "pause", "edit", "source_pause", "self"]
)
def test_owner_changes_during_await_discard_facts_and_followups(
    loop, app, agent_id, tmp_path, phase, change
):
    watch = loop.watch()
    loop.phase = phase
    loop.decision["followup"] = {
        "kind": "topic_review",
        "title": "Parser follow-up",
        "interval_seconds": 120,
    }

    def mutate():
        if change == "scope":
            app.activities.revoke_scope(watch["scope_id"], expected_revision=1)
        elif change == "origin":
            app.activities.configure_web_origins(agent_id, [], expected_revision=1)
        elif change == "permissions":
            app.permissions.configure(
                agent_id, readable=[tmp_path], writable=[], expected_revision=0
            )
        elif change in {"pause", "source_pause"}:
            item = watch if change == "source_pause" else loop.item
            app.activities.pause(item["id"], expected_revision=1)
        elif change == "edit":
            app.activities.update(loop.item["id"], title="New intent", expected_revision=1)
        else:
            ref = app.history.add(
                agent_id,
                "Owner prefers compact updates",
                kind="user",
                scope="real",
                request_id="owner-change",
            )
            app.identity.propose(
                agent_id,
                kind="habit",
                key="communication.response_length",
                text="concise",
                sources=[ref],
                base_revision=0,
                request_id="habit-change",
            )
            assert app.identity.current(agent_id).revision == 1

    loop.mutate = mutate
    loop.tick()
    assert observations(app) == [] and app.activities.notifications(agent_id) == []
    assert not [a for a in app.activities.list(agent_id) if a["origin"] == "model"]
    if phase == "source":
        assert loop.requests == []


@pytest.mark.parametrize("kind", ["file_watch", "web_watch"])
def test_paused_source_is_not_explored_through_another_attention(loop, app, agent_id, kind):
    watch = loop.watch(kind)
    app.activities.pause(watch["id"], expected_revision=1)
    loop.tick()
    assert observations(app) == [] and loop.requests == [] and loop.web_requests == []


@pytest.mark.parametrize(
    "followup",
    [
        {
            "kind": "web_watch",
            "title": "New URL",
            "target": ORIGIN + "/other",
            "interval_seconds": 60,
        },
        {"kind": "web_watch", "title": "Duplicate", "target": URL, "interval_seconds": 60},
        {"kind": "topic_review", "title": "Compiler progress", "interval_seconds": 60},
    ],
)
def test_followup_cannot_widen_scope_or_duplicate_active_activity(loop, app, agent_id, followup):
    loop.watch()
    loop.decision["followup"] = followup
    assert loop.tick()["outcome"] == "changed"
    assert not [a for a in app.activities.list(agent_id) if a["origin"] == "model"]
    assert len(app.activities.scopes(agent_id)) == 1


def test_revisit_creates_one_model_review_without_notification_or_personal_change(
    loop, app, agent_id
):
    loop.watch()
    loop.decision = {
        "changed": False,
        "summary": "Revisit parser compatibility after testing.",
        "notification": "Must be suppressed while waiting",
        "followup": {
            "kind": "topic_review",
            "title": "Parser compatibility",
            "interval_seconds": 120,
        },
    }
    assert loop.tick()["outcome"] == "unchanged"
    created = [a for a in app.activities.list(agent_id) if a["origin"] == "model"]
    assert len(created) == 1 and created[0]["interval_seconds"] == 120
    assert created[0]["kind"] == "topic_review" and created[0]["target"] is None
    assert app.activities.notifications(agent_id) == []
    assert app.identity.current(agent_id).revision == 0 and app.development.list(agent_id) == []
    assert len(observations(app)) == 1


def test_relevant_bounded_memories_and_current_goals_reach_model(loop, app, agent_id):
    loop.watch()
    ref = app.history.add(
        agent_id, "Compiler parser work", kind="user", scope="real", request_id="compiler-brief"
    )
    app.memory.knowledge.save(
        agent_id,
        "Compiler parser project uses incremental tests.",
        [ref],
        kind="fact",
        request_id="compiler-memory",
    )
    app.memory.knowledge.save(
        agent_id, "Unrelated gardening memory", [ref], kind="fact", request_id="unrelated-memory"
    )
    goal = app.development.propose_goal(
        agent_id, "Compiler project", [ref], request_id="compiler-goal"
    )
    app.development.decide(goal["id"], "activate", expected_revision=1)
    loop.tick()
    data = loop.data()
    assert "incremental tests" in data["memories"] and "gardening" not in data["memories"]
    assert len(data["memories"]) < 3000
    assert data["goals"][0]["title"] == "Compiler project"
    assert data["goals"][0]["status"] == "active"
