"""Real gateway/MockTransport: live compaction, semantic recall and restart."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from lingnexus.common.errors import Conflict, RevisionConflict
from lingnexus.common.types import new_id
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime
from lingnexus.runtime.compaction import CompactionBlocked, CompactionService, assert_closed


class WorkHooks:
    """B isolates runner mechanics while A's separate source tests evolve."""

    def __init__(self, work, main=None):
        self.work = work
        self.main_id = main
        self.captures = []
        self.assertions = []
        self.answers = []
        self.revoked = False

    def is_work_conversation(self, conversation_id):
        return conversation_id == self.work

    def branch_for_conversation(self, conversation_id):
        return {"objective": "Read the discovered DECISION and create answer.txt with that exact value; verify it.", "id": "branch-fixture"} if self.is_work_conversation(conversation_id) else None

    def project_messages(self, agent, conversation, messages):
        return messages, []

    def capture_run_dependencies(self, run, ids):
        self.captures.append((run, ids))

    def assert_run_sources(self, run):
        self.assertions.append(run)
        if self.revoked:
            raise Conflict("revoked")

    def inherit_answer_dependencies(self, run, answer):
        self.answers.append((run, answer))


def configure(app, family, *, window=50000):
    profile = ModelProfile(api_family=family, model="epoch-fixture", base_url="https://fixture.test", context_window_tokens=window, max_output_tokens=1000)
    saved = app.profiles.save("epoch-fixture", profile, api_key="epoch-fixture-secret")
    app.profiles.select(saved["id"], None)
    return saved["id"], profile


def response(family, name=None, args=None, *, call_id=None, text="done"):
    call_id = call_id or new_id()
    if family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        delta = {"role": "assistant", "content": text, "reasoning_content": "PRIVATE_CHAIN_MARK"}
        if name:
            delta["tool_calls"] = [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
        frames = [{"id": new_id(), "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if name else "stop"}]}]
        return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    if family == APIFamily.OPENAI_RESPONSES:
        output = [{"type": "reasoning", "id": new_id(), "encrypted_content": "PRIVATE_CHAIN_MARK", "summary": []}]
        if name:
            output.append({"type": "function_call", "id": new_id(), "call_id": call_id, "name": name, "arguments": json.dumps(args), "status": "completed"})
        else:
            output.append({"type": "message", "id": new_id(), "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        # Text delta is required for display/final persistence by this parser.
        if not name:
            frames = [{"type": "response.created", "response": {"id": call_id, "status": "in_progress", "output": []}},
                      {"type": "response.output_item.added", "output_index": 0, "item": {"id": output[1]["id"], "type": "message", "role": "assistant", "content": []}},
                      {"type": "response.output_text.delta", "output_index": 0, "item_id": output[1]["id"], "delta": text},
                      {"type": "response.completed", "response": {"id": call_id, "status": "completed", "output": output}}]
        else:
            frames = [{"type": "response.completed", "response": {"id": call_id, "status": "completed", "output": output}}]
    else:
        frames = [{"type": "message_start", "message": {"id": new_id(), "role": "assistant", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "private", "signature": "PRIVATE_CHAIN_MARK"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": text}},
                  {"type": "content_block_stop", "index": 1}]
        if name:
            frames.extend([{"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": args}}, {"type": "content_block_stop", "index": 2}])
        frames.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use" if name else "end_turn"}, "usage": {"output_tokens": 1}}, {"type": "message_stop"}])
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)


def native_results(body, family):
    if family == APIFamily.OPENAI_RESPONSES:
        return [json.loads(item["output"]) for item in body["input"] if item.get("type") == "function_call_output"]
    if family == APIFamily.ANTHROPIC_MESSAGES:
        return [json.loads(block["content"]) for item in body["messages"] if isinstance(item.get("content"), list) for block in item["content"] if block.get("type") == "tool_result"]
    return [json.loads(item["content"]) for item in body["messages"] if item.get("role") == "tool"]


async def drain(runtime, run_id):
    return [event async for event in runtime.events(run_id)]


@pytest.mark.parametrize("family", list(APIFamily))
def test_two_epochs_semantic_recall_new_effect_complete_restart_next_turn(app, agent_id, tmp_path, family):
    pid, profile = configure(app, family)
    work = app.conversations.create(agent_id, "work")["id"]
    main = app.conversations.create(agent_id, "main")["id"]
    before_main = app.conversations.append_user(main, "untouched main", request_id="main-before")
    hooks = WorkHooks(work, main)
    app.continuity = hooks
    root = tmp_path / "authorized"
    root.mkdir()
    app.permissions.configure(agent_id, readable=[root], writable=[root], expected_revision=0)
    fact = root / "facts.txt"
    fact.write_text("padding " * 250 + "\nDECISION=blue\n" + "older detail " * 1900 + "OLD_DETAIL_SENTINEL", encoding="utf-8")
    target = root / "answer.txt"
    requests, states = [], []
    service = CompactionService(app)
    service.configure(agent_id, expected_revision=0, enabled=True, profile_modes={pid: "safe_epoch_restart"}, max_summary_bytes=24576)

    async def scenario():
        index = 0
        run_id = None
        def handler(request):
            nonlocal index
            body = json.loads(request.content)
            requests.append(body)
            states.append(runtime.checkpoints.load(run_id))
            if index < 4:
                stream = response(family, "read_file", {"path": str(fact)}, call_id=f"read-{index}", text="Inspect decision source before producing the artifact.")
            elif index == 4:
                cp = states[-1]
                assert cp["context_epoch"] >= 2
                assert "OLD_DETAIL_SENTINEL" not in json.dumps(body)
                assert "PRIVATE_CHAIN_MARK" not in json.dumps(body)
                stream = response(family, "event_records", {"event_id": "run:" + run_id, "limit": 20}, call_id="directory")
            elif index == 5:
                directory = native_results(body, family)[-1]
                source = next(item for item in directory["items"] if item["kind"] == "tool_result" and item["call_id"] == "read-0")
                stream = response(family, "event_read", {"event_id": "run:" + run_id, "source_id": source["id"], "max_bytes": 4096}, call_id="read-fact-page")
            elif index == 6:
                read = native_results(body, family)[-1]
                # The correct action depends on a fact beyond the handoff excerpt.
                assert "DECISION=blue" in read["text"]
                stream = response(family, "create_file", {"path": str(target), "text": "blue"}, call_id="write-once")
            else:
                stream = response(family, text="Created blue artifact; work remains subject to verification.")
            index += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, conversation_id=work, message="Read decision and create answer; preserve the remaining verification work.", request_id="epochs"))
            run_id = run["id"]
            events = await drain(runtime, run_id)
            assert runtime.get_run(run_id)["status"] == "completed", (
                events, [e for e in events if e.get("event") in ("notice", "error")]
            )
            cp = runtime.checkpoints.load(run_id)
            assert cp["context_epoch"] >= 2
            assert cp["step"] == 8
            assert target.read_text() == "blue"
            assert hooks.captures and hooks.answers and len(hooks.assertions) > 20
            with app.database.transaction() as db:
                effects = db.execute("SELECT step,call_id,status FROM chat_tool_effects WHERE run_id=?", (run_id,)).fetchall()
                assert len(effects) == 7 and all(e["status"] == "completed" for e in effects)
                assert sum(e["call_id"] == "write-once" for e in effects) == 1
                epochs = db.execute("SELECT state FROM chat_context_epochs WHERE run_id=? ORDER BY epoch", (run_id,)).fetchall()
                assert len(epochs) >= 2
                assert "PRIVATE_CHAIN_MARK" in epochs[0][0]
                assert "OLD_DETAIL_SENTINEL" in epochs[0][0]
                assert db.execute("SELECT count(*) FROM chat_context_reads WHERE run_id=?", (run_id,)).fetchone()[0] == 2
            replay = runtime.checkpoints.completed_items(cp["assistant_message_id"], profile.model_dump(mode="json"))
            assert replay[0]["content"] == cp["replay_prefix"][0]["content"]
            assert replay[1:] == cp["payload"].get("input", cp["payload"].get("messages"))[cp["initial_length"]:]
            await runtime.aclose()
        followups = []
        def next_handler(request):
            followups.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=response(family, text="Continue verification."))
        async with httpx.AsyncClient(transport=httpx.MockTransport(next_handler)) as client:
            restarted = ChatRuntime(app, client=client)
            following = restarted.submit(ChatRequest(agent_id=agent_id, conversation_id=work, message="continue", request_id="next-epoch-turn"))
            ev = await drain(restarted, following["id"])
            assert restarted.get_run(following["id"])["status"] == "completed", ev
            await restarted.aclose()
        encoded = json.dumps(followups[0], ensure_ascii=False)
        assert "进行中工作交接" in encoded and "remaining" in encoded
        assert "OLD_DETAIL_SENTINEL" not in encoded
        assert encoded.index("进行中工作交接") < encoded.index("read-fact-page")
        assert app.conversations.read(main)["head_id"] == before_main["id"]
    asyncio.run(scenario())


def test_policy_defaults_validation_and_revision_cas(app, agent_id):
    pid, _ = configure(app, APIFamily.OPENAI_CHAT_COMPLETIONS)
    service = CompactionService(app)
    assert service.settings(agent_id)["enabled"] is False
    assert service.settings(agent_id)["profile_modes"] == {}
    value = service.configure(agent_id, expected_revision=0, enabled=True, profile_modes={pid: "safe_epoch_restart"})
    assert value["revision"] == 1 and value["validation"] == "offline_only"
    with pytest.raises(RevisionConflict):
        service.configure(agent_id, expected_revision=0)
    with pytest.raises(ValueError):
        service.configure(agent_id, expected_revision=1, profile_modes={pid: "native_compaction"})
    assert service.settings(agent_id) == value


@pytest.mark.parametrize("family", list(APIFamily))
def test_pending_native_group_is_not_edited_and_parallel_group_can_close(family):
    from lingnexus.models import ToolCall
    from lingnexus.runtime.payload import append_tool_calls, append_tool_results
    profile = ModelProfile(api_family=family, model="fixture", base_url="https://fixture.test")
    key = "input" if family == APIFamily.OPENAI_RESPONSES else "messages"
    payload = {key: [{"role": "user", "content": "goal"}]}
    calls = [ToolCall("one", "read_file", "{}", {}), ToolCall("two", "read_file", "{}", {})]
    append_tool_calls(payload, profile, calls)
    before = deepcopy(payload)
    with pytest.raises(CompactionBlocked):
        assert_closed(payload[key], family)
    assert payload == before
    append_tool_results(payload, profile, [(c, "fact") for c in calls])
    assert_closed(payload[key], family)
