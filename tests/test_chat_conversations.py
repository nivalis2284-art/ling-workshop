"""Conversation chat integration; only the external model transport is mocked."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from test_chat_runtime import (
    FAMILIES,
    GatedStream,
    chat_api,  # noqa: F401 - shared owner-authenticated API fixture
    configure,
    decode_events,
    owner,
    success,
)

from lingnexus.common.errors import Conflict, DomainError, PermissionDenied, SourceInvalid
from lingnexus.common.types import SourceRef
from lingnexus.runtime import ChatRequest, ChatRuntime


@asynccontextmanager
async def model_runtime(app, calls, *, family="openai_chat_completions", stream=None):
    def handler(req):
        calls.append(json.loads(req.content))
        body = {"stream": stream} if stream else {"content": success(family)}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, **body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = ChatRuntime(app, client=client)
        try:
            yield runtime
        finally:
            await runtime.aclose()


def turn(agent_id, request_id, message="Hi", conversation_id=None):
    return ChatRequest(
        agent_id=agent_id,
        request_id=request_id,
        message=message,
        conversation_id=conversation_id,
    )


async def complete(runtime, value):
    run = runtime.submit(value)
    events = [event async for event in runtime.events(run["id"])]
    assert events[-1]["event"] == "complete", events
    return runtime.get_run(run["id"])


def test_same_agent_conversations_keep_separate_transcripts(app, agent_id):
    configure(app)
    a = app.conversations.create(agent_id, "A")["id"]
    b = app.conversations.create(agent_id, "B")["id"]
    calls = []

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            first = await complete(runtime, turn(agent_id, "a1", "A secret", a))
            second = await complete(runtime, turn(agent_id, "b1", "B secret", b))
            third = await complete(runtime, turn(agent_id, "a2", "A follow up", a))
            assert first["conversation_id"] == third["conversation_id"] == a
            assert second["conversation_id"] == b
            assert third["context_sources"] == [
                first["user_source"],
                first["assistant_source"],
                third["user_source"],
            ]
            assert second["context_sources"] == [second["user_source"]]
            assert [r["text"] for r in app.conversations.messages(b)] == ["B secret", "Hello 灵"]

    asyncio.run(scenario())
    assert calls[1]["messages"][1:] == [{"role": "user", "content": "B secret"}]
    assert calls[2]["messages"][1:] == [
        {"role": "user", "content": "A secret"},
        {"role": "assistant", "content": "Hello 灵"},
        {"role": "user", "content": "A follow up"},
    ]


def test_default_is_persistent_and_replay_does_not_resolve_current_selection(app, agent_id):
    configure(app)
    calls = []
    value = turn(agent_id, "original")

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            first = await complete(runtime, value)
            cid = first["conversation_id"]
            assert app.conversations.read(cid)["scope"] == ""
            edited = app.conversations.edit_user(
                cid, first["user_message_id"], "Edited", request_id="edit"
            )
            other = app.conversations.create(agent_id, "New active conversation")["id"]
            await complete(runtime, turn(agent_id, "explicit", "Elsewhere", other))
            before = app.conversations.messages(cid)
            assert runtime.submit(value) == first
            assert app.conversations.messages(cid) == before
            assert app.conversations.read(cid)["head_id"] == edited["id"]
            with pytest.raises(Conflict):
                runtime.submit(value.model_copy(update={"conversation_id": cid}))
            with pytest.raises(Conflict):
                runtime.submit(value.model_copy(update={"conversation_id": other}))
        # A new manager must reuse the persisted default, not the newest conversation.
        async with model_runtime(app, calls) as reopened:
            next_run = await complete(reopened, turn(agent_id, "follow-up", "Next"))
            assert next_run["conversation_id"] == cid
            assert [m["text"] for m in app.conversations.messages(cid)] == [
                "Edited",
                "Next",
                "Hello 灵",
            ]
            app.profiles.clear_credentials()
            assert reopened.submit(value) == first

    asyncio.run(scenario())
    assert len(calls) == 3


@pytest.mark.parametrize("family", FAMILIES)
def test_new_real_conversation_preserves_current_personality(app, agent_id, family):
    configure(app, family)
    evidence = app.history.add(
        agent_id, "Growth evidence", kind="user", scope="real", request_id="growth"
    )
    proposal = app.identity.propose(
        agent_id,
        kind="identity",
        key="identity.description",
        text="Patient and curious",
        sources=[evidence],
        base_revision=0,
        request_id="personality",
    )
    app.identity.decide(proposal["id"], "approve", expected_revision=0)
    snapshot = app.identity.current(agent_id)
    real = app.conversations.create(agent_id, "Real")["id"]
    calls = []

    async def scenario():
        async with model_runtime(app, calls, family=family) as runtime:
            run = await complete(runtime, turn(agent_id, real, "Hello", real))
            assert run["context_sources"] == [run["user_source"]]
            for field in ("user_source", "assistant_source"):
                assert app.history.read(SourceRef(**run[field]))["scope"] == ""
            assert app.identity.current(agent_id) == snapshot

    asyncio.run(scenario())
    instructions = [
        body.get("system") or body.get("instructions") or body["messages"][0]["content"]
        for body in calls
    ]
    assert all("Patient and curious" in text for text in instructions)
    assert "当前交流情境" not in instructions[0]
    assert "real" not in instructions[0]
    assert "虚构" not in instructions[0]
    assert "Growth evidence" not in json.dumps(calls)


@pytest.mark.parametrize("change", ["edit", "append", "select_ancestor"])
def test_head_change_midrun_prevents_assistant_commit(app, agent_id, change):
    configure(app)
    cid = app.conversations.create(agent_id, "Branch")["id"]
    ancestor = app.conversations.append_user(cid, "Ancestor", request_id="ancestor")
    calls = []

    async def scenario():
        stream = GatedStream()
        async with model_runtime(app, calls, stream=stream) as runtime:
            run = runtime.submit(turn(agent_id, "pending", "Question", cid))
            await asyncio.wait_for(stream.waiting.wait(), 2)
            if change == "edit":
                head = app.conversations.edit_user(
                    cid, run["user_message_id"], "Replacement", request_id="edit"
                )["id"]
            elif change == "append":
                # All captured refs remain active: only the head check can reject this.
                head = app.conversations.append_user(cid, "New head", request_id="new")["id"]
                app.history.validate_refs(
                    agent_id, [SourceRef(**r) for r in run["context_sources"]], "real"
                )
            else:
                head = ancestor["id"]
                app.conversations.select_head(cid, head)
            before = app.history.count(agent_id)
            stream.release.set()
            events = [e async for e in runtime.events(run["id"])]
            assert events[-1]["data"]["code"] == "context_invalidated"
            assert runtime.get_run(run["id"])["status"] == "failed"
            assert runtime.get_run(run["id"])["assistant_source"] is None
            assert app.conversations.read(cid)["head_id"] == head
            assert app.history.count(agent_id) == before
            assert [e async for e in runtime.events(run["id"])] == events

    asyncio.run(scenario())


def test_select_old_branch_explicitly_is_the_only_history_used(app, agent_id):
    configure(app)
    cid = app.conversations.create(agent_id, "Branch")["id"]
    old = app.conversations.append_user(cid, "Original question", request_id="old")
    answer = app.conversations.append_assistant(
        cid, "Original answer", request_id="answer", expected_head=old["id"]
    )
    edited = app.conversations.edit_user(cid, old["id"], "Other branch", request_id="edit")
    app.conversations.select_head(cid, answer["id"])
    calls = []

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            run = await complete(runtime, turn(agent_id, "selected", "Continue", cid))
            assert run["context_sources"] == [
                old["source"].model_dump(),
                answer["source"].model_dump(),
                run["user_source"],
            ]
            with pytest.raises(SourceInvalid):
                app.history.read(edited["source"])

    asyncio.run(scenario())
    assert [m["content"] for m in calls[0]["messages"][1:]] == [
        "Original question",
        "Original answer",
        "Continue",
    ]


@pytest.mark.parametrize("change", ["approve", "correct", "pending"])
def test_self_revision_is_captured_and_revalidated_during_generation(app, agent_id, change):
    configure(app)
    evidence = app.history.add(
        agent_id, "Independent identity evidence", kind="user", scope="real", request_id="self"
    )
    proposal = app.identity.propose(
        agent_id,
        kind="identity",
        key="identity.description",
        text="Patient and curious",
        sources=[evidence],
        base_revision=0,
        request_id="personality",
    )
    if change == "correct":
        app.identity.decide(proposal["id"], "approve", expected_revision=0)
    revision = app.identity.current(agent_id).revision
    calls = []

    async def scenario():
        stream = GatedStream()
        async with model_runtime(app, calls, stream=stream) as runtime:
            run = runtime.submit(turn(agent_id, "pending"))
            assert run["self_revision"] == revision
            assert run["context_sources"] == [run["user_source"]]
            await asyncio.wait_for(stream.waiting.wait(), 2)
            if change == "approve":
                app.identity.decide(proposal["id"], "approve", expected_revision=0)
            elif change == "correct":
                app.history.correct(evidence, "Corrected identity evidence")
            stream.release.set()
            events = [e async for e in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            assert result["self_revision"] == revision
            if change == "pending":
                assert events[-1]["event"] == "complete"
            else:
                assert events[-1]["data"]["code"] == "context_invalidated"
                assert result["assistant_source"] is None
                assert len(app.conversations.messages(run["conversation_id"])) == 1

    asyncio.run(scenario())


def test_raw_source_correction_keeps_message_revision_and_rejects_stale_path(app, agent_id):
    configure(app)
    cid = app.conversations.create(agent_id, "Corrections")["id"]
    calls = []

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            original = await complete(runtime, turn(agent_id, "original", "Old text", cid))
            ref = SourceRef(**original["user_source"])
            corrected = app.history.correct(ref, "Corrected evidence")
            nodes = app.conversations.messages(cid)
            assert nodes[0]["source"] == ref
            assert nodes[0]["text"] == "Old text"
            assert app.history.read(corrected)["text"] == "Corrected evidence"
            with pytest.raises(SourceInvalid):
                runtime.submit(turn(agent_id, "stale", "Next", cid))
            assert app.conversations.messages(cid) == nodes
            assert app.history.count(agent_id) == 2
            assert runtime.submit(turn(agent_id, "original", "Old text", cid)) == original
            # Explicit editing replaces the stale branch; refs in the old run stay immutable.
            app.conversations.edit_user(
                cid, original["user_message_id"], "Revised question", request_id="replace"
            )
            fresh = await complete(runtime, turn(agent_id, "fresh", "Next", cid))
            assert ref.model_dump() not in fresh["context_sources"]
            assert corrected.model_dump() not in fresh["context_sources"]
            assert runtime.get_run(original["id"])["context_sources"] == [ref.model_dump()]

    asyncio.run(scenario())
    assert len(calls) == 2


@pytest.mark.parametrize("explicit", [False, True])
def test_credentials_preflight_does_not_create_sources_messages_or_default(app, agent_id, explicit):
    configure(app, key=None)
    cid = app.conversations.create(agent_id, "Existing")["id"] if explicit else None
    before = app.conversations.list(agent_id)
    calls = []

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            with pytest.raises(PermissionDenied):
                runtime.submit(turn(agent_id, "no-key", conversation_id=cid))
            assert app.conversations.list(agent_id) == before
            assert app.history.count(agent_id) == 0
            assert app.history.pending_jobs(agent_id) == []
            if cid:
                assert app.conversations.messages(cid) == []

    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize("problem", ["other_agent", "missing"])
def test_explicit_conversation_preflight_validates_ownership_and_scope(app, agent_id, problem):
    configure(app)
    other = app.create_agent("Other")
    cid = app.conversations.create(other if problem == "other_agent" else agent_id, "Target")["id"]
    if problem == "missing":
        cid = "missing"
    calls = []

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            with pytest.raises(PermissionDenied if problem == "other_agent" else DomainError):
                runtime.submit(turn(agent_id, "denied", conversation_id=cid))
            assert app.history.count(agent_id) == app.history.count(other) == 0

    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_api_conversation_run_and_branch_inspection_contract(chat_api, prefix):  # noqa: F811
    client, calls = chat_api
    app = client.app.state.services
    agent = app.create_agent("Fixture")
    configure(app)
    cid = client.post(
        prefix + "/conversations", headers=owner(), json={"agent_id": agent, "title": "Chat"}
    ).json()["id"]
    value = {"agent_id": agent, "message": "Hi", "request_id": "api", "conversation_id": cid}
    events = decode_events(client.post(prefix + "/chat/stream", headers=owner(), json=value))
    assert events[0]["data"]["conversation_id"] == cid
    run_id = events[0]["data"]["run_id"]
    run = client.get(prefix + f"/chat/runs/{run_id}", headers=owner()).json()
    assert run["conversation_id"] == cid
    nodes = client.get(prefix + f"/conversations/{cid}/messages", headers=owner()).json()
    assert [n["role"] for n in nodes] == ["user", "assistant"]
    assert nodes[0]["id"] == run["user_message_id"]
    edited = client.post(
        prefix + f"/conversations/{cid}/edit",
        headers=owner(),
        json={"message_id": nodes[0]["id"], "content": "Edit", "request_id": "edit"},
    ).json()
    for path in [f"/conversations/{cid}", f"/conversations/{cid}/branches"]:
        assert client.get(prefix + path).status_code == 401
    conversation = client.get(prefix + f"/conversations/{cid}", headers=owner()).json()
    assert conversation["head_id"] == edited["id"]
    branches = client.get(prefix + f"/conversations/{cid}/branches", headers=owner()).json()
    assert {node["id"] for node in branches} == {nodes[1]["id"], edited["id"]}
    assert [node["id"] for node in branches if node["selected"]] == [edited["id"]]
    archived = client.get(
        prefix + f"/conversations/{cid}/messages",
        headers=owner(),
        params={"head_id": nodes[1]["id"]},
    ).json()
    assert archived == nodes
    assert (
        decode_events(client.post(prefix + "/chat/stream", headers=owner(), json=value)) == events
    )
    assert len(calls) == 1
    other = app.create_agent("Other")
    assert (
        client.post(
            prefix + "/chat/stream", headers=owner(), json=value | {"agent_id": other}
        ).status_code
        == 403
    )


def test_pre_conversation_run_replays_without_inventing_membership(app, agent_id):
    """Nullable migration fields must preserve legacy fingerprints and event frames."""
    from lingnexus.common.types import utc_now

    profile_id = configure(app)
    value = turn(agent_id, "legacy")
    legacy_fingerprint = hashlib.sha256(
        value.model_dump_json(exclude={"conversation_id"}).encode()
    ).hexdigest()
    ref = app.history.add(agent_id, "Hi", kind="user", scope="real", request_id="legacy-source")
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "user_source_id,user_source_revision,context_sources,created_at) "
            "VALUES (?,?,?,?,?,'completed',?,?,?,?)",
            (
                "legacy-run",
                agent_id,
                "legacy",
                legacy_fingerprint,
                profile_id,
                ref.id,
                ref.revision,
                json.dumps([ref.model_dump()]),
                utc_now(),
            ),
        )
        db.execute(
            "INSERT INTO chat_events VALUES (?,?,?,?,?)",
            (
                "legacy-run",
                1,
                "complete",
                json.dumps(
                    {"run_id": "legacy-run", "status": "completed", "assistant_source": None}
                ),
                utc_now(),
            ),
        )
    app.profiles.clear_credentials()
    calls = []

    async def scenario():
        async with model_runtime(app, calls) as runtime:
            run = runtime.submit(value)
            assert run["id"] == "legacy-run"
            assert run["conversation_id"] is run["user_message_id"] is run["self_revision"] is None
            events = [e async for e in runtime.events(run["id"])]
            assert [e["event"] for e in events] == ["complete"]
            assert app.conversations.list(agent_id) == []
            assert app.history.count(agent_id) == 1

    asyncio.run(scenario())
    assert calls == []
