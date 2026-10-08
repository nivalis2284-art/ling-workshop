"""CHAT-only failover/regeneration contracts at the mocked HTTP boundary."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from test_chat_runtime import KEY, OPAQUE, chunk, success, wire

from lingnexus.common.errors import DomainError, PermissionDenied
from lingnexus.common.types import SourceRef
from lingnexus.models import ModelCapabilities, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime


def profiles(app, *, backup_family="openai_chat_completions", backup_effort=()):
    def save(name, family, effort):
        return app.profiles.save(
            name,
            ModelProfile(
                api_family=family,
                model=name,
                base_url=f"https://{name}.invalid",
                capabilities=ModelCapabilities(effort_levels=effort),
                max_output_tokens=128,
            ),
            api_key=KEY,
        )["id"]

    main = save("main", "openai_chat_completions", ("low", "high"))
    backup = save("backup", backup_family, backup_effort)
    manual = save("manual", "openai_chat_completions", ())
    app.profiles.select(main, backup)
    return main, backup, manual


def turn(agent_id, request_id="turn", **kwargs):
    return ChatRequest(agent_id=agent_id, message="Question", request_id=request_id, **kwargs)


@asynccontextmanager
async def runtime_for(app, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = ChatRuntime(app, client=client)
        try:
            yield runtime
        finally:
            await runtime.aclose()


async def run_turn(runtime, value):
    run = runtime.submit(value)
    events = [e async for e in runtime.events(run["id"])]
    assert sum(e["event"] in ("complete", "error") for e in events) == 1
    return runtime.get_run(run["id"]), events


def ok(family="openai_chat_completions", text="Backup answer"):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=success(family, text)
    )


def test_main_failure_binds_only_this_conversation_across_managers(app, agent_id, app_factory):
    main, backup, _ = profiles(app)
    calls = []

    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(503) if len(calls) == 1 else ok()

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            first, events = await run_turn(runtime, turn(agent_id))
            assert first["profile_id"] == backup
            assert first["initial_profile_id"] == main
            assert first["status"] == "completed"
            switch = next(e for e in events if e["event"] == "model_switch")
            assert switch["data"] == {
                "run_id": first["id"],
                "from_profile": main,
                "to_profile": backup,
                "restart": True,
            }
            assert next(e for e in events if e["event"] == "notice")["id"] < switch["id"]
            app.profiles.clear_credentials()
            assert runtime.submit(turn(agent_id)) == first
        # A fresh Application proves the binding is not an in-memory profile choice.
        reopened = app_factory(app.database.path)
        try:
            for pid in (main, backup):
                saved = reopened.profiles.get(pid)
                reopened.profiles.save(
                    saved["name"], ModelProfile(**saved["profile"]), api_key=KEY, profile_id=pid
                )
            async with runtime_for(reopened, handler) as runtime:
                second, _ = await run_turn(runtime, turn(agent_id, "next", profile_id=main))
                assert second["profile_id"] == backup
                new = reopened.conversations.create(agent_id, "New")["id"]
                third, _ = await run_turn(runtime, turn(agent_id, "new", conversation_id=new))
                assert third["profile_id"] == main
        finally:
            reopened.close()

    asyncio.run(scenario())
    assert calls == ["main.invalid", "backup.invalid", "backup.invalid", "main.invalid"]


def test_manual_other_profile_overrides_binding_and_never_fails_over(app, agent_id):
    _, backup, manual = profiles(app)
    calls = []

    def handler(req):
        calls.append(req.url.host)
        return ok() if req.url.host == "backup.invalid" else httpx.Response(503)

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            first, _ = await run_turn(runtime, turn(agent_id))
            selected, events = await run_turn(runtime, turn(agent_id, "manual", profile_id=manual))
            assert selected["profile_id"] == manual
            assert events[-1]["event"] == "error"
            assert not any(e["event"] == "model_switch" for e in events)
            later, _ = await run_turn(runtime, turn(agent_id, "later"))
            assert first["profile_id"] == later["profile_id"] == backup

    asyncio.run(scenario())
    assert calls == ["main.invalid", "backup.invalid", "manual.invalid", "backup.invalid"]


@pytest.mark.parametrize("same_profile", [False, True])
def test_all_failed_is_one_terminal_no_loop_or_same_profile(app, agent_id, same_profile):
    main, backup, _ = profiles(app)
    if same_profile:
        app.profiles.select(main, main)
    calls = []

    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(503)

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            run, events = await run_turn(runtime, turn(agent_id))
            assert run["status"] == "failed" and run["assistant_source"] is None
            assert events[-1]["data"]["code"] == "http_error"
            assert run["profile_id"] == (main if same_profile else backup)
            assert [e async for e in runtime.events(run["id"])] == events

    asyncio.run(scenario())
    assert len(calls) == (1 if same_profile else 2)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
def test_http_errors_without_clear_unavailability_are_terminal(app, agent_id, status):
    profiles(app)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status)

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            _, events = await run_turn(runtime, turn(agent_id))
            assert events[-1]["data"]["code"] == "http_error"
            assert not any(e["event"] == "model_switch" for e in events)

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "transport", "eof", "provider_busy"])
def test_clearly_unavailable_stream_can_switch(app, agent_id, failure):
    profiles(app)
    calls = []

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "backup.invalid":
            return ok()
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture", request=req)
        if failure == "transport":
            raise httpx.ConnectError("fixture", request=req)
        body = (
            wire({"error": {"type": "overloaded_error", "message": OPAQUE}})
            if failure == "provider_busy"
            else wire(chunk({"content": "Partial"}))
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            _, events = await run_turn(runtime, turn(agent_id))
            assert events[-1]["event"] == "complete"
            assert OPAQUE not in json.dumps(events)

    asyncio.run(scenario())
    assert calls == ["main.invalid", "backup.invalid"]


@pytest.mark.parametrize(
    "failure",
    [
        "filter",
        "truncated",
        "malformed",
        "provider",
        "tools",
        "filter_eof",
        "length_eof",
        "refusal_eof",
    ],
)
def test_non_availability_stream_failures_never_switch(app, agent_id, failure):
    profiles(app)
    calls = []
    bodies = {
        "filter": success("openai_chat_completions", finish_reason="content_filter"),
        "truncated": success("openai_chat_completions", finish_reason="length"),
        "malformed": b"data: {broken\n\n",
        "provider": wire({"error": {"type": "invalid_request_error", "message": OPAQUE}}),
        "tools": success("openai_chat_completions", finish_reason="tool_calls"),
        "filter_eof": wire(chunk(finish="content_filter")),
        "length_eof": wire(chunk(finish="length")),
        "refusal_eof": wire(chunk({"refusal": "Refused"})),
    }

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=bodies[failure]
        )

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            _, events = await run_turn(runtime, turn(agent_id))
            assert events[-1]["event"] == "error"
            assert not any(e["event"] == "model_switch" for e in events)

    asyncio.run(scenario())
    assert len(calls) == 1


def test_partial_output_restart_fresh_native_payload_and_per_call_usage(app, agent_id):
    main, backup, _ = profiles(app, backup_family="anthropic_messages")
    calls = []
    partial = wire(
        {
            **chunk(
                {
                    "reasoning_content": "Main thinking",
                    "content": "Main partial",
                    "opaque_signature": OPAQUE,
                }
            ),
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        },
        {
            "id": "chat_fixture",
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
    )

    def handler(req):
        calls.append(json.loads(req.content))
        return (
            httpx.Response(200, headers={"content-type": "text/event-stream"}, content=partial)
            if len(calls) == 1
            else ok("anthropic_messages")
        )

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            run, events = await run_turn(runtime, turn(agent_id, effort="high"))
            display = {"text": "", "thinking": ""}
            for e in events:
                if e["event"] == "model_switch" and e["data"]["restart"]:
                    display = {"text": "", "thinking": ""}
                elif e["event"] in display:
                    display[e["event"]] += e["data"]["text"]
            assert display == {"text": "Backup answer", "thinking": "Visible summary"}
            assert app.history.read(SourceRef(**run["assistant_source"]))["text"] == "Backup answer"
            assert any(e["data"].get("text") == "Main partial" for e in events)
            usage = [e["data"] for e in events if e["event"] == "usage"]
            assert [(u["attempt"], u["profile_id"]) for u in usage] == [
                (1, main),
                (1, main),
                (2, backup),
                (2, backup),
            ]
            assert run["attempts"][0]["usage"]["total_tokens"] == 10
            assert run["attempts"][1]["usage"]["output_tokens"] == 4
            assert run["usage"]["input_tokens"] == 17
            assert run["usage"]["output_tokens"] == 7
            assert [a["status"] for a in run["attempts"]] == ["failed", "completed"]
            notices = [e["data"] for e in events if e["event"] == "notice"]
            assert any(n["code"] == "effort_omitted" and n["profile_id"] == backup for n in notices)
            with app.database.transaction() as db:
                dump = "\n".join(db.iterdump())
            assert KEY not in dump and OPAQUE not in dump

    asyncio.run(scenario())
    assert calls[1]["messages"] == [{"role": "user", "content": "Question"}]
    assert "output_config" not in calls[1] and "thinking" not in calls[1]
    assert OPAQUE not in json.dumps(calls[1])


@pytest.mark.parametrize("change", ["source", "edit", "head", "self"])
def test_context_change_before_failover_prevents_fresh_call(app, agent_id, change):
    profiles(app)
    cid = app.conversations.create(agent_id, "Original")["id"]
    calls = []

    def handler(req):
        calls.append(req)
        node = app.conversations.messages(cid)[-1]
        if change == "source":
            app.history.correct(node["source"], "Changed")
        elif change == "edit":
            app.conversations.edit_user(cid, node["id"], "Changed", request_id="edit")
        elif change == "head":
            app.conversations.append_user(cid, "New head", request_id="head")
        else:
            proposal = app.identity.propose(
                agent_id,
                kind="identity",
                key="identity.description",
                text="Changed",
                sources=[node["source"]],
                base_revision=0,
                request_id="self",
            )
            app.identity.decide(proposal["id"], "approve", expected_revision=0)
        return httpx.Response(503)

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            run, events = await run_turn(runtime, turn(agent_id, conversation_id=cid))
            assert events[-1]["data"]["code"] == "context_invalidated"
            assert run["profile_id"] == app.profiles.selection()["primary"]
            assert not any(e["event"] == "model_switch" for e in events)

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("lost", ["main_before_submit", "backup_mid_failure", "bound_backup"])
def test_lost_credentials_are_local_and_never_trigger_loop(app, agent_id, lost):
    main, backup, _ = profiles(app)
    calls = []

    def clear(pid):
        item = app.profiles.get(pid)
        app.profiles.save(item["name"], ModelProfile(**item["profile"]), profile_id=pid, api_key="")

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "main.invalid":
            if lost == "backup_mid_failure":
                clear(backup)
            return httpx.Response(503)
        return ok()

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            if lost == "main_before_submit":
                clear(main)
            run, events = await run_turn(runtime, turn(agent_id))
            if lost == "backup_mid_failure":
                assert events[-1]["data"]["code"] == "credentials_unavailable"
            else:
                assert run["profile_id"] == backup and events[-1]["event"] == "complete"
            if lost == "bound_backup":
                clear(backup)
                before = app.history.count(agent_id)
                with pytest.raises(PermissionDenied):
                    runtime.submit(turn(agent_id, "next"))
                assert app.history.count(agent_id) == before

    asyncio.run(scenario())
    assert len(calls) == (2 if lost == "bound_backup" else 1)


def test_regenerate_reuses_user_and_selects_new_answer_branch(app, agent_id):
    profiles(app)
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return ok(text=f"Answer {len(calls)}")

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            original, _ = await run_turn(runtime, turn(agent_id))
            cid, uid = original["conversation_id"], original["user_message_id"]
            value = turn(agent_id, "regenerate", conversation_id=cid, regenerate_message_id=uid)
            generated, _ = await run_turn(runtime, value)
            assert generated["user_source"] == original["user_source"]
            assert generated["user_message_id"] == uid
            assert [n["text"] for n in app.conversations.messages(cid)] == ["Question", "Answer 2"]
            branches = app.conversations.branches(cid)
            assert len(branches) == 2
            assert [n["text"] for n in branches if n["selected"]] == ["Answer 2"]
            with app.database.transaction() as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM conversation_messages WHERE role='user'"
                    ).fetchone()[0]
                    == 1
                )
            app.profiles.clear_credentials()
            assert runtime.submit(value) == generated

    asyncio.run(scenario())
    assert calls[1]["messages"][1:] == [{"role": "user", "content": "Question"}]


@pytest.mark.parametrize("problem", ["conversation", "text", "assistant", "off_branch", "keys"])
def test_regenerate_validation_rolls_back_without_retiring_answer(app, agent_id, problem):
    main, _, _ = profiles(app)

    async def scenario():
        async with runtime_for(app, lambda _: ok()) as runtime:
            original, _ = await run_turn(runtime, turn(agent_id))
            cid, uid = original["conversation_id"], original["user_message_id"]
            changes = {}
            if problem == "conversation":
                changes["conversation_id"] = None
            elif problem == "text":
                changes["message"] = "Different"
            elif problem == "assistant":
                uid = app.conversations.messages(cid)[-1]["id"]
            elif problem == "off_branch":
                app.conversations.edit_user(cid, uid, "Other", request_id="edit")
            else:
                app.profiles.clear_credentials()
                app.profiles.select(main, None)
            before = app.conversations.messages(cid)
            count = app.history.count(agent_id)
            # Internal callers can use model_copy; submit must enforce this too.
            if problem == "conversation":
                value = turn(
                    agent_id,
                    "bad-regen",
                    conversation_id=cid,
                    regenerate_message_id=uid,
                ).model_copy(update={"conversation_id": None})
            else:
                value = ChatRequest(
                    agent_id=agent_id,
                    request_id="bad-regen",
                    regenerate_message_id=uid,
                    **({"conversation_id": cid, "message": "Question"} | changes),
                )
            with pytest.raises((DomainError, ValueError)):
                runtime.submit(value)
            assert app.conversations.messages(cid) == before
            assert app.history.count(agent_id) == count

    asyncio.run(scenario())


@pytest.mark.parametrize("explicit_conversation", [False, True])
def test_null_regenerate_preserves_legacy_fingerprint(app, agent_id, explicit_conversation):
    profiles(app)
    cid = app.conversations.create(agent_id, "Explicit")["id"] if explicit_conversation else None

    async def scenario():
        async with runtime_for(app, lambda _: ok()) as runtime:
            value = turn(agent_id, conversation_id=cid, regenerate_message_id=None)
            excluded = {"regenerate_message_id"} | ({"conversation_id"} if cid is None else set())
            fingerprint = hashlib.sha256(
                value.model_dump_json(exclude=excluded).encode()
            ).hexdigest()
            run, _ = await run_turn(runtime, value)
            with app.database.transaction() as db:
                assert (
                    db.execute(
                        "SELECT fingerprint FROM chat_runs WHERE id=?", (run["id"],)
                    ).fetchone()[0]
                    == fingerprint
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["complete", "failover", "unchanged"])
def test_recall_manifest_is_persisted_deduped_and_validated(app, agent_id, phase):
    profiles(app)
    evidence = app.history.add(
        agent_id,
        "Question original excerpt",
        kind="observation",
        scope="real",
        request_id="evidence",
    )
    app.memory.save_episode(
        agent_id,
        "Question memory",
        "Question report",
        [evidence],
        request_id="episode",
    )
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if phase != "unchanged":
            app.history.correct(evidence, "Revised excerpt")
        return httpx.Response(503) if phase == "failover" else ok()

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            run, events = await run_turn(runtime, turn(agent_id))
            assert run["context_manifest"]["recall_sources"] == [evidence.model_dump()]
            assert run["context_sources"] == [run["user_source"], evidence.model_dump()]
            nodes = app.conversations.messages(run["conversation_id"])
            assert all(n["source"] != evidence for n in nodes)
            if phase == "unchanged":
                assert events[-1]["event"] == "complete" and len(nodes) == 2
            else:
                assert events[-1]["data"]["code"] == "context_invalidated" and len(nodes) == 1
            assert len(calls) == 1

    asyncio.run(scenario())
    assert "Question original excerpt" in json.dumps(calls[0])


def test_anthropic_signature_is_not_replayed_to_responses_backup(app, agent_id):
    main, _, _ = profiles(app, backup_family="openai_responses")
    item = app.profiles.get(main)
    profile = ModelProfile(**(item["profile"] | {"api_family": "anthropic_messages"}))
    app.profiles.save("main", profile, profile_id=main, api_key=KEY)
    calls = []
    partial = success("anthropic_messages", "Abandoned answer").removesuffix(
        wire({"type": "message_stop"})
    )

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=partial
            )
        return ok("openai_responses")

    async def scenario():
        async with runtime_for(app, handler) as runtime:
            _, events = await run_turn(runtime, turn(agent_id))
            assert events[-1]["event"] == "complete"
            assert [e["event"] for e in events].count("model_switch") == 1

    asyncio.run(scenario())
    assert calls[1]["input"] == [{"role": "user", "content": "Question"}]
    assert "previous_response_id" not in calls[1]
    assert OPAQUE not in json.dumps(calls[1])
