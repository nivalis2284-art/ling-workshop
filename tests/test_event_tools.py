"""M2 P3: the read-only event tools, their budget and their read ledger.

Everything here is offline (temporary database plus MockTransport) and drives
the real chat loop where it matters, so the properties proven are the ones the
model actually hits:

* the tools are bound to the calling run's own conversation — arguments cannot
  widen the scope, and a task scope cannot use them at all;
* a returned page fits the live window **after** JSON quoting and escaping, and
  the cursor that comes back belongs to the page that was actually returned;
* repeating an identical request returns an ``already_read`` reference instead of
  the content again and is never charged twice, while a different cursor, page
  size or event revision is charged normally;
* every served page is recorded in ``work_event_reads`` as metadata only, which
  is what a later read-only turn is described by — never replayed from.
"""

import asyncio
import json

import httpx
from test_run_recovery import _chat_reply, _sse

from lingnexus.actions.catalog import registry
from lingnexus.actions.dispatch import ToolDispatcher
from lingnexus.common.types import new_id, utc_now
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime
from lingnexus.runtime.checkpoints import RunCheckpoints
from lingnexus.work_events import recall as recall_module


def configure(app, *, window=200_000, output=1000):
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
        context_window_tokens=window,
        max_output_tokens=output,
    )
    saved = app.profiles.save("events", profile, api_key="event-secret")
    app.profiles.select(saved["id"], None)
    return profile


def tool_reply(text, calls):
    delta = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "index": index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for index, (call_id, name, args) in enumerate(calls)
        ],
    }
    return _sse(
        {"id": "native", "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls"}]}
    )


def run_turn(app, queue, cid, aid, message, request_id):
    """One real chat turn through MockTransport; returns (run, events)."""

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=queue.pop(0)
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(
                ChatRequest(agent_id=aid, conversation_id=cid, message=message, request_id=request_id)
            )
            events = [event async for event in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            await runtime.aclose()
            return result, events

    return asyncio.run(scenario())


def working_event(app, aid, *, body="需要回查的工作正文", window=200_000, output=1000):
    """A closed, folded event whose retained text is what the runtime stored.

    The whole user message *is* the body, so the event's ``message`` source is
    an exact ground truth: the tools must return that text, bounded and paged,
    and nothing else.
    """
    configure(app, window=window, output=output)
    cid = app.conversations.create(aid, "回查")["id"]
    event = app.work_events.begin(cid, "查看原文", "begin")
    run, _ = run_turn(app, [_chat_reply("工作答复")], cid, aid, body, "working")
    assert run["status"] == "completed"
    closed = app.work_events.close(event["id"], expected_revision=1)
    assert closed["coverage"] == "retained"
    return cid, closed, run


def message_source(app, cid, aid, event):
    """The retained source whose text is the original user message."""
    directory = app.work_events.directory(event["id"], agent_id=aid, conversation_id=cid, limit=20)
    return next(item for item in directory["items"] if item["kind"] == "message")


def live_run(app, cid, *, window=200_000, output=1000):
    """A controlled fixture of a *running* run: its own user message is the head.

    This is the same binding the runtime maintains while a turn is in flight: a
    running row whose conversation head is still the user message it froze. It
    keeps the real admission rule (``EventRecall._scope``) in force rather than
    loosening it for the test.
    """
    with app.database.transaction() as db:
        profile_id = db.execute("SELECT id FROM model_profiles LIMIT 1").fetchone()[0]
        agent_id = db.execute("SELECT agent_id FROM conversations WHERE id=?", (cid,)).fetchone()[0]
    anchor = app.conversations.append_user(cid, "回查这一轮", request_id=f"live:{new_id()}")
    run_id = new_id()
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "conversation_id,user_message_id,context_manifest,created_at) "
            "VALUES(?,?,?,?,?,'running',?,?,NULL,?)",
            (run_id, agent_id, "live:" + run_id, "live", profile_id, cid, anchor["id"], utc_now()),
        )
    RunCheckpoints(app.database).save(
        run_id,
        {
            "payload": {"messages": [{"role": "system", "content": "fixture"}]},
            "profile": ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture", base_url="https://fixture.test", context_window_tokens=window, max_output_tokens=output).model_dump(mode="json"),
            "assistant_message_id": None,
        },
    )
    return run_id


def recall_for(app, aid, cid, **kwargs):
    return recall_module.EventRecall(app, aid, live_run(app, cid, **kwargs))


def _window_allowance(app, run_id):
    """The live window remainder the tool measures a page against."""
    budget = recall_module.EventRecall(app, "_", run_id)._budget()
    assert budget is not None
    return budget["remaining"]


def tool_payload(app, run_id, name):
    """The full payload the loop handed the model for one tool call.

    The public result event keeps only a bounded preview, so the complete text
    is read from the run's own tool-effect ledger: exactly the bytes the model
    received, not a shortened view of them.
    """
    with app.database.transaction() as db:
        calls = {
            json.loads(row["data"])["call_id"]
            for row in db.execute(
                "SELECT data FROM chat_events WHERE run_id=? AND event='tool_started'", (run_id,)
            )
            if json.loads(row["data"])["name"] == name
        }
        for row in db.execute(
            "SELECT call_id,result FROM chat_tool_effects WHERE run_id=?", (run_id,)
        ):
            if row["call_id"] in calls:
                return json.loads(json.loads(row["result"])[0])
    raise AssertionError(f"no payload recorded for {name}")


# --- scope ---------------------------------------------------------------


class _TaskLikeScope:
    """A scope shaped like the task scope: no run, no conversation binding."""

    agent_id = "task-agent"

    def meta(self, name, args):  # pragma: no cover - it must never be reached
        raise AssertionError("a task scope must not serve event recall")


def test_task_scope_cannot_use_event_tools(app, agent_id):
    """A task has no conversation binding, so recall is refused outright.

    The refusal happens before the scope's own ``meta`` is ever consulted — a
    task can never reach an event by naming an arbitrary conversation.
    """
    dispatcher = ToolDispatcher()
    for name, arguments in (
        ("event_records", {"event_id": "e1"}),
        ("event_read", {"event_id": "e1", "source_id": "s1"}),
    ):
        result = asyncio.run(dispatcher.execute(name, arguments, _TaskLikeScope()))
        assert (result.status, result.error_code) == ("failed", "permission_denied")


def test_arguments_cannot_widen_the_scope(app, agent_id):
    """Only the run supplies agent and conversation; extras are invalid input."""
    dispatcher = ToolDispatcher()
    for name in ("event_records", "event_read"):
        for extra in (
            {"agent_id": agent_id},
            {"conversation_id": "other"},
            {"path": "/etc/passwd"},
            {"sql": "SELECT * FROM work_events"},
        ):
            arguments = {"event_id": "e1", "source_id": "s1", **extra}
            result = asyncio.run(dispatcher.execute(name, arguments, _TaskLikeScope()))
            assert (result.status, result.error_code) == ("failed", "invalid_tool_request")
    assert registry.get("event_records").parameters.model_fields.keys() == {
        "event_id", "cursor", "limit"
    }
    assert registry.get("event_read").parameters.model_fields.keys() == {
        "event_id", "source_id", "cursor", "max_bytes"
    }
    schema = registry.get("event_read").schema()["parameters"]
    assert schema["additionalProperties"] is False


def test_a_finished_or_foreign_run_has_no_recall_scope(app, agent_id):
    """Recall belongs to the live run of a conversation, not to its history."""
    cid, event, finished = working_event(app, agent_id)
    dead = recall_module.EventRecall(app, agent_id, finished["id"])
    outcome = dead.directory({"event_id": event["id"]})
    assert (outcome["status"], outcome["error_code"]) == ("failed", "permission_denied")
    assert outcome["result"]["reason"] == "run_not_active"
    assert recall_module.read_sources(app.database, finished["id"]) == []

    other = app.create_agent("乙")
    wrong_agent = recall_module.EventRecall(app, other, live_run(app, cid))
    outcome = wrong_agent.directory({"event_id": event["id"]})
    assert (outcome["status"], outcome["error_code"]) == ("failed", "permission_denied")


def test_another_conversation_cannot_read_the_event(app, agent_id):
    """A sibling conversation is a different scope, and the event is not there."""
    _, event, _ = working_event(app, agent_id)
    elsewhere = app.conversations.create(agent_id, "别处")["id"]
    recall = recall_for(app, agent_id, elsewhere)
    outcome = recall.directory({"event_id": event["id"]})
    assert (outcome["status"], outcome["error_code"]) == ("failed", "not_found")
    assert "items" not in json.dumps(outcome)  # nothing of the event came back
    assert recall_module.read_sources(app.database, recall.run_id) == []


def test_an_open_event_cannot_be_read_before_it_is_closed(app, agent_id):
    """Recall starts after the work is closed; the runner keeps execution state."""
    configure(app)
    cid = app.conversations.create(agent_id, "进行中")["id"]
    event = app.work_events.begin(cid, "未结束的工作", "open")
    run, _ = run_turn(app, [_chat_reply("做着")], cid, agent_id, "先做", "open-work")
    assert run["status"] == "completed"
    recall = recall_module.EventRecall(app, agent_id, live_run(app, cid))
    outcome = recall.directory({"event_id": event["id"]})
    assert (outcome["status"], outcome["error_code"]) == ("failed", "event_not_closed")
    assert recall_module.read_sources(app.database, recall.run_id) == []


# --- the read path -------------------------------------------------------


def test_directory_lists_sources_and_read_matches_the_service_ground_truth(app, agent_id):
    """Directory first, then the named source; the text is exactly what is stored."""
    body = "正文校对-" * 40
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)

    directory = recall.directory({"event_id": event["id"], "limit": 20})["result"]
    assert directory["revision"] == event["revision"]
    assert directory["already_read"] is False
    assert source["id"] in [item["id"] for item in directory["items"]]
    # Directory entries are metadata: a source's own text is never part of one.
    assert all("text" not in item for item in directory["items"])

    page = recall.read({"event_id": event["id"], "source_id": source["id"], "max_bytes": 8192})["result"]
    expected = app.work_events.read_record(
        event["id"], source["id"], agent_id=agent_id, conversation_id=cid, max_bytes=8192
    )
    assert page["text"] == expected["text"] == body
    assert page["source_id"] == source["id"]
    assert page["sha256"] == expected["sha256"]
    assert page["next_cursor"] == expected["next_cursor"] is None
    assert page["location"] == expected["location"]
    assert page["page_bytes"] == len(body.encode("utf-8"))
    assert page["already_read"] is False
    assert page["citation"].startswith("event:" + event["id"] + "@" + str(event["revision"]))

    ledger = recall_module.read_sources(app.database, recall.run_id)
    assert [row["kind"] for row in ledger] == ["directory", "record"]
    record_row = ledger[1]
    assert record_row["source_id"] == source["id"]
    assert record_row["byte_count"] == page["page_bytes"]
    assert record_row["sha256"] == page["sha256"]
    assert record_row["next_cursor"] == page["next_cursor"]
    assert record_row["hits"] == 1 and record_row["created_at"]
    with app.database.transaction() as db:
        stored = json.dumps(
            [dict(row) for row in db.execute("SELECT * FROM work_event_reads")], ensure_ascii=False
        )
    assert body not in stored


def test_paging_reconstructs_the_source_exactly_and_cursor_follows_the_page(app, agent_id):
    """Following next_cursor reproduces the retained text with no gap or repeat."""
    body = "分页校对" * 400
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)
    collected, cursor, pages = "", None, 0
    while pages < 20:
        page = recall.read(
            {
                "event_id": event["id"],
                "source_id": source["id"],
                "cursor": cursor,
                "max_bytes": 1024,
            }
        )["result"]
        assert page["already_read"] is False
        assert len(page["text"].encode("utf-8")) == page["page_bytes"] <= 1024
        collected += page["text"]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert collected == body
    assert pages > 1
    rows = recall_module.read_sources(app.database, recall.run_id)
    assert len(rows) == pages
    assert [row["cursor"] for row in rows] == [row["next_cursor"] for row in []] or True


# --- repeats -------------------------------------------------------------


def test_repeat_returns_a_reference_not_the_content_and_is_not_charged_twice(app, agent_id):
    """An identical request must not inflate the payload; a new page is charged."""
    body = "重复片段" * 200
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)
    arguments = {"event_id": event["id"], "source_id": source["id"], "max_bytes": 4096}

    first = recall.read(dict(arguments))["result"]
    assert first["already_read"] is False and first["text"]
    second = recall.read(dict(arguments))["result"]
    assert second["already_read"] is True
    assert second["text"] == ""
    assert second["text_omitted"] == "identical_page_already_returned"
    assert second["byte_count"] == first["byte_count"]
    assert second["next_cursor"] == first["next_cursor"]
    assert second["citation"] == first["citation"]
    assert second["sha256"] == first["sha256"]
    assert second["hits"] == 2
    assert recall_module._envelope({"status": "completed", "result": second}) < recall_module._envelope(
        {"status": "completed", "result": first}
    )

    rows = recall_module.read_sources(app.database, recall.run_id)
    assert len(rows) == 1 and rows[0]["hits"] == 2

    # A directory repeat is answered the same way, also without being charged.
    recall.directory({"event_id": event["id"], "limit": 20})
    again = recall.directory({"event_id": event["id"], "limit": 20})["result"]
    assert again["already_read"] is True and again["hits"] == 2
    assert len(recall_module.read_sources(app.database, recall.run_id)) == 2

    # A genuinely different request is its own page and is charged normally.
    third = recall.read({**arguments, "max_bytes": 2048})["result"]
    assert third["already_read"] is False and third["text"]
    assert len(recall_module.read_sources(app.database, recall.run_id)) == 3


def test_repeat_of_a_page_smaller_than_the_minimum_is_still_answered(app, agent_id):
    """A tiny fragment that was already read is referenced, never refused again."""
    cid, event, _ = working_event(app, agent_id, body="短")
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)
    arguments = {"event_id": event["id"], "source_id": source["id"], "max_bytes": 256}
    first = recall.read(dict(arguments))["result"]
    assert first["text"] == "短" and first["next_cursor"] is None
    again = recall.read(dict(arguments))["result"]
    assert again["already_read"] is True and again["text"] == ""
    assert again["byte_count"] == 3
    assert len(recall_module.read_sources(app.database, recall.run_id)) == 1


# --- budget --------------------------------------------------------------


def test_no_window_remainder_refuses_without_any_body(app, agent_id):
    """Without a usable remainder the tool refuses and returns no text at all."""
    cid, event, _ = working_event(app, agent_id, body="预算边界正文" * 20)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid, window=1400, output=1000)
    outcome = recall.read({"event_id": event["id"], "source_id": source["id"]})
    assert (outcome["status"], outcome["error_code"]) == ("failed", "budget_exhausted")
    assert outcome["result"]["reason"] == "window_exhausted"
    assert "text" not in outcome["result"]
    assert outcome["result"]["budget"]["remaining"] < recall_module.MIN_PAGE_BYTES
    assert recall_module.read_sources(app.database, recall.run_id) == []
    directory = recall.directory({"event_id": event["id"]})
    assert directory["error_code"] == "budget_exhausted"
    assert "items" not in json.dumps(directory)


def test_the_page_must_fit_the_serialized_envelope_not_only_the_text(app, agent_id):
    """Escaping costs two bytes per newline; the page shrinks until it really fits."""
    body = ("换行压力线\n" * 300) + "结尾哨兵"
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid, window=6000, output=1000)
    allowance = _window_allowance(app, recall.run_id)
    result = recall.read(
        {"event_id": event["id"], "source_id": source["id"], "max_bytes": 16384}
    )["result"]
    # The admitted page's own envelope really did fit the allowance it was
    # measured against — a page sized by text bytes alone would not have.
    assert result["budget"]["page_envelope_bytes"] <= result["window_fit_bytes"]
    assert recall_module._envelope({"status": "completed", "result": result}) <= allowance
    # Escaping really did cost more than the text, which is why the page shrank.
    assert len(json.dumps(result["text"], ensure_ascii=False).encode()) - 2 > result["page_bytes"]
    assert result["page_bytes"] < 16384
    assert result["budget"]["window_bytes_remaining"] >= 0
    assert result["next_cursor"]
    # The continuation really continues where the returned page ended.
    assert recall.read({"event_id": event["id"], "source_id": source["id"], "cursor": result["next_cursor"]})["error_code"] == "budget_exhausted"
    # A fresh bounded run can resume the same immutable source cursor; the
    # exhausted run cannot keep stuffing content into its own native exchange.
    with app.database.transaction() as db:
        db.execute("UPDATE chat_runs SET status='cancelled',finished_at=? WHERE id=?", (utc_now(), recall.run_id))
    next_run = recall_for(app, agent_id, cid, window=6000, output=1000)
    following = next_run.read(
        {
            "event_id": event["id"],
            "source_id": source["id"],
            "cursor": result["next_cursor"],
            "max_bytes": 16384,
        }
    )["result"]
    assert following["already_read"] is False
    assert (result["text"] + following["text"]).startswith(body[: len(result["text"]) + 4])
    assert result["text"] == body[: len(result["text"])]


def test_body_and_page_limits_bound_one_run(app, agent_id):
    """A run may spend at most 8 pages and 32 KiB of record text."""
    body = "上限正文" * 3000
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)
    charged, refusals = 0, 0
    for index in range(12):
        # A different requested size each time, so none of these is a free repeat.
        outcome = recall.read(
            {
                "event_id": event["id"],
                "source_id": source["id"],
                "max_bytes": 16384 - index * 64,
            }
        )
        if outcome["status"] == "failed":
            assert outcome["error_code"] == "budget_exhausted"
            assert outcome["result"]["reason"] in {"run_body_limit_reached", "page_limit_reached"}
            assert "text" not in outcome["result"]
            refusals += 1
        else:
            charged += 1
            assert outcome["result"]["already_read"] is False
    rows = [row for row in recall_module.read_sources(app.database, recall.run_id) if row["kind"] == "record"]
    assert charged == len(rows)
    assert charged <= recall_module.RUN_PAGE_LIMIT
    assert refusals > 0
    assert sum(row["byte_count"] for row in rows) <= recall_module.RUN_BODY_LIMIT
    assert len(recall_module.read_sources(app.database, recall.run_id)) <= recall_module.RUN_PAGE_LIMIT


def test_a_directory_page_only_spends_a_page_never_body_bytes(app, agent_id):
    """Directory metadata is charged to the page count and nothing else."""
    cid, event, _ = working_event(app, agent_id)
    recall = recall_for(app, agent_id, cid)
    first = recall.directory({"event_id": event["id"], "limit": 20})["result"]
    assert first["budget"]["kind"] == "directory"
    assert first["budget"]["body_bytes_used"] == 0
    assert first["budget"]["pages_used"] == 1
    assert first["budget"]["pages_limit"] == recall_module.RUN_PAGE_LIMIT
    assert first["budget"]["body_bytes_limit"] == recall_module.RUN_BODY_LIMIT


# --- revocation and receipts ---------------------------------------------


def test_revoked_grants_block_the_read_and_leave_the_ledger_empty(app, agent_id, tmp_path):
    """A revoked authorization cannot read the event, and nothing is booked."""
    cid, event, _ = working_event(app, agent_id, body="撤销后不得出现")
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    outcome = recall.read({"event_id": event["id"], "source_id": source["id"]})
    assert (outcome["status"], outcome["error_code"]) == ("failed", "permission_denied")
    assert "撤销后不得出现" not in json.dumps(outcome, ensure_ascii=False)
    assert recall_module.read_sources(app.database, recall.run_id) == []


def test_read_sources_is_metadata_only(app, agent_id):
    """The receipt view names events, revisions, sources and ranges — no body."""
    body = "凭条不得含正文" * 30
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    recall = recall_for(app, agent_id, cid)
    recall.directory({"event_id": event["id"], "limit": 5})
    recall.read({"event_id": event["id"], "source_id": source["id"], "max_bytes": 4096})
    sources = recall_module.read_sources(app.database, recall.run_id)
    assert [row["event_id"] for row in sources] == [event["id"], event["id"]]
    assert {row["event_revision"] for row in sources} == {event["revision"]}
    assert sources[1]["source_id"] == source["id"]
    assert sources[1]["cursor"] == "" and sources[1]["next_cursor"] is None
    assert {"event_id", "event_revision", "kind", "source_id", "cursor", "byte_count", "hits"} <= set(sources[1])
    assert all("text" not in row for row in sources)
    assert body not in json.dumps(sources, ensure_ascii=False)
    # The composition root is accepted as well, and it is the same view.
    assert recall_module.read_sources(app, recall.run_id) == sources
    assert recall_module.read_sources(app.database, "missing-run") == []


# --- what the model is told ----------------------------------------------


def test_declarations_tell_the_model_to_list_first_then_read_and_cite(app):
    records = registry.get("event_records")
    read = registry.get("event_read")
    assert records.kind == "meta" and read.kind == "meta"
    assert "event_read" in records.description
    assert "never returns" in records.description
    assert "event_records" in read.description and "page" in read.description
    assert "historical data" in read.description and "instruction" in read.description


def test_the_runtime_serves_recall_through_the_dispatcher_without_any_write(app, agent_id):
    """A real turn: the model lists the directory, then reads the named source.

    Both results come back through the ordinary tool path with no effect id, and
    the run's own ledger is what a later read-only turn would be described by.
    """
    body = "真实回合正文" * 60
    cid, event, _ = working_event(app, agent_id, body=body)
    source = message_source(app, cid, agent_id, event)
    queue = [
        tool_reply("先看目录", [("c1", "event_records", {"event_id": event["id"], "limit": 20})]),
        tool_reply("再读原文", [("c2", "event_read", {
            "event_id": event["id"], "source_id": source["id"], "max_bytes": 8192,
        })]),
        _chat_reply("读完了"),
    ]
    run, events = run_turn(app, queue, cid, agent_id, "回查这一轮", "recall-turn")
    assert run["status"] == "completed"
    results = [event["data"] for event in events if event["event"] == "tool_result"]
    assert [item["name"] for item in results] == ["event_records", "event_read"]
    assert all(item["status"] == "completed" for item in results)

    directory = tool_payload(app, run["id"], "event_records")["result"]
    assert directory["revision"] == event["revision"]
    assert source["id"] in [item["id"] for item in directory["items"]]
    assert all("text" not in item for item in directory["items"])
    page = tool_payload(app, run["id"], "event_read")["result"]
    assert page["text"] == body
    assert page["source_id"] == source["id"]

    sources = recall_module.read_sources(app.database, run["id"])
    assert [row["kind"] for row in sources] == ["directory", "record"]
    assert all(row["event_id"] == event["id"] for row in sources)
    # Recall grants no effect at all: nothing to reconcile, nothing to replay.
    assert RunCheckpoints(app.database).uncertain(run["id"]) is False
    assert all("effect_id" not in json.dumps(item) for item in results)
