"""Acceptance tests for the work-event semantic record store (M2 P1).

The store only has to be honest about four things: who a capture belongs to,
that an identical replay is idempotent while changed content is refused, that
the body is stored whole (never truncated) as byte chunks, and that a page
request returns a UTF-8-safe slice with a byte-accurate continuation offset.
Attribution and the authorization snapshot come from durable run state, not
from the caller, so withdrawing a grant later cannot re-label an old capture.
"""

import hashlib
import json

import pytest

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.common.types import new_id, utc_now
from lingnexus.runtime.checkpoints import RunCheckpoints
from lingnexus.work_events.store import (
    CHUNK_BYTES,
    MAX_PAGE_BYTES,
    EventRecordStore,
)


def make_run(app, agent_id, conversation_id, *, request_id=None):
    """A completed durable chat run, exactly as the runtime leaves one behind."""
    run_id = new_id()
    profile_id = new_id()
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO model_profiles VALUES(?,?,?)",
            (profile_id, "fixture", json.dumps({"model": "fixture"})),
        )
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "conversation_id,user_message_id,context_manifest,created_at,finished_at) "
            "VALUES(?,?,?,?,?,'completed',?,?,?,?,?)",
            (
                run_id,
                agent_id,
                request_id or new_id(),
                "fingerprint",
                profile_id,
                conversation_id,
                None,
                None,
                utc_now(),
                utc_now(),
            ),
        )
    return run_id


def read_all(store, record_id, *, max_bytes=8192):
    """Every page of a record, in order, with paging invariants checked."""
    text, offset, pages = "", 0, 0
    while True:
        page = store.read(record_id, offset=offset, max_bytes=max_bytes)
        assert page["sha256"] == store.read(record_id, offset=0)["sha256"]
        assert len(page["text"].encode("utf-8")) <= max_bytes
        text += page["text"]
        pages += 1
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
        assert pages < 1000
    return text


# --- attribution ---------------------------------------------------------


def test_record_binds_the_real_run_agent_and_conversation(app, agent_id):
    conversation = app.conversations.create(agent_id, "工作")
    run_id = make_run(app, agent_id, conversation["id"])
    store = EventRecordStore(app)
    stored = store.record(
        run_id, "tool_result", "命令输出：ok", step=2, call_id="c1", metadata={"name": "run_command"}
    )
    assert stored["agent_id"] == agent_id
    assert stored["conversation_id"] == conversation["id"]
    assert stored["run_id"] == run_id
    assert stored["byte_count"] == len("命令输出：ok".encode())
    assert stored["sha256"] == hashlib.sha256("命令输出：ok".encode()).hexdigest()
    assert stored["metadata"] == {"name": "run_command"}
    assert stored["coverage"] == "retained"
    fetched = store.for_runs([run_id])
    assert [item["id"] for item in fetched] == [stored["id"]]
    assert "text" not in fetched[0]  # the directory never loads bodies
    assert store.read(stored["id"])["text"] == "命令输出：ok"


def test_record_refuses_an_unknown_run_and_invalid_input(app, agent_id):
    store = EventRecordStore(app)
    with pytest.raises(NotFound):
        store.record(new_id(), "tool_result", "x")
    conversation = app.conversations.create(agent_id, "工作")
    run_id = make_run(app, agent_id, conversation["id"])
    with pytest.raises(ValueError):
        store.record(run_id, "", "x")
    with pytest.raises(TypeError):
        store.record(run_id, "tool_result", 42)
    with pytest.raises(ValueError):
        store.record(run_id, "tool_result", "x", step=-1)
    with pytest.raises(ValueError):
        store.record(run_id, "tool_result", "x", metadata={"huge": "y" * 70_000})


def test_legacy_run_without_conversation_keeps_an_empty_scope(app, agent_id):
    run_id = make_run(app, agent_id, None)
    stored = EventRecordStore(app).record(run_id, "guidance", "改用另一个目录")
    assert stored["conversation_id"] == ""


# --- idempotency ---------------------------------------------------------


def test_identical_replay_is_idempotent_and_changed_text_conflicts(app, agent_id):
    conversation = app.conversations.create(agent_id, "工作")
    run_id = make_run(app, agent_id, conversation["id"])
    store = EventRecordStore(app)
    first = store.record(run_id, "tool_call", '{"argv":["ls"]}', step=1, call_id="c1")
    again = store.record(run_id, "tool_call", '{"argv":["ls"]}', step=1, call_id="c1")
    assert again["id"] == first["id"]
    assert again["reused"] is True
    assert [item["id"] for item in store.for_runs([run_id])] == [first["id"]]
    with pytest.raises(Conflict):
        store.record(run_id, "tool_call", '{"argv":["rm"]}', step=1, call_id="c1")
    other = store.record(run_id, "tool_call", '{"argv":["ls"]}', step=1, call_id="c2")
    assert other["id"] != first["id"]
    second_run = make_run(app, agent_id, conversation["id"])
    elsewhere = store.record(second_run, "tool_call", '{"argv":["ls"]}', step=1, call_id="c1")
    assert elsewhere["id"] != first["id"]


def test_records_survive_a_restart_with_metadata_intact(app_factory, app, agent_id):
    conversation = app.conversations.create(agent_id, "重启")
    run_id = make_run(app, agent_id, conversation["id"])
    stored = EventRecordStore(app).record(
        run_id,
        "guide",
        "先看文档再动手",
        metadata={"source": "owner", "tags": ["指导"]},
    )
    again = app_factory(app.database.path)
    try:
        replayed = EventRecordStore(again).read(stored["id"])
        assert replayed["text"] == "先看文档再动手"
        assert replayed["metadata"] == {"source": "owner", "tags": ["指导"]}
        assert replayed["sha256"] == stored["sha256"]
        assert replayed["location"] == {
            "run_id": run_id,
            "kind": "guide",
            "step": 0,
            "call_id": "",
            "attempt": 0,
            "chunk_bytes": CHUNK_BYTES,
        }
    finally:
        again.close()


# --- chunking and paging -------------------------------------------------


def test_large_unicode_body_pages_back_whole_with_matching_hash(app, agent_id):
    conversation = app.conversations.create(agent_id, "大输出")
    run_id = make_run(app, agent_id, conversation["id"])
    body = "汉字ab🚀" * 100_000  # ~1 MiB of mixed-width text across many chunks
    assert len(body.encode("utf-8")) > 1_000_000
    stored = EventRecordStore(app).record(run_id, "tool_result", body, step=3, call_id="big")
    assert stored["byte_count"] == len(body.encode("utf-8"))
    assert stored["byte_count"] > CHUNK_BYTES * 10  # stored as many chunks, whole
    with app.database.transaction() as db:
        chunks = db.execute(
            "SELECT COUNT(*) FROM work_event_chunks WHERE record_id=?", (stored["id"],)
        ).fetchone()[0]
    assert chunks == -(-stored["byte_count"] // CHUNK_BYTES)
    raw = read_all(EventRecordStore(app), stored["id"], max_bytes=MAX_PAGE_BYTES)
    assert raw == body
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == stored["sha256"]


def test_pages_do_not_split_wide_characters_or_exceed_the_limit(app, agent_id):
    conversation = app.conversations.create(agent_id, "分页")
    run_id = make_run(app, agent_id, conversation["id"])
    body = "灵" * 50  # 3 bytes each: a 4-byte page must still cut on a boundary
    stored = EventRecordStore(app).record(run_id, "visible_text", body)
    store = EventRecordStore(app)
    first = store.read(stored["id"], max_bytes=4)
    assert first["text"] == "灵"
    assert first["next_offset"] == 3
    second = store.read(stored["id"], offset=first["next_offset"], max_bytes=4)
    assert second["text"] == "灵"
    with pytest.raises(ValueError):
        store.read(stored["id"], offset=1)
    with pytest.raises(ValueError):
        store.read(stored["id"], max_bytes=MAX_PAGE_BYTES + 1)
    with pytest.raises(ValueError):
        store.read(stored["id"], max_bytes=1)


def test_full_body_reassembles_across_a_max_size_page(app, agent_id):
    conversation = app.conversations.create(agent_id, "跨块")
    run_id = make_run(app, agent_id, conversation["id"])
    # Neither piece fits a page: the page must end mid-body with a continuation.
    body = "A" * (CHUNK_BYTES + 10) + "终"
    stored = EventRecordStore(app).record(run_id, "tool_call", body, step=1, call_id="c")
    store = EventRecordStore(app)
    page = store.read(stored["id"], max_bytes=MAX_PAGE_BYTES)
    assert page["text"] == "A" * MAX_PAGE_BYTES
    assert page["next_offset"] == MAX_PAGE_BYTES
    assert read_all(store, stored["id"], max_bytes=MAX_PAGE_BYTES) == body
    tail = store.read(stored["id"], offset=stored["byte_count"])
    assert tail["text"] == ""
    assert tail["next_offset"] is None


def test_empty_body_is_valid_and_pages_once(app, agent_id):
    conversation = app.conversations.create(agent_id, "空答复")
    run_id = make_run(app, agent_id, conversation["id"])
    stored = EventRecordStore(app).record(run_id, "visible_text", "")
    assert stored["byte_count"] == 0
    page = EventRecordStore(app).read(stored["id"])
    assert page["text"] == "" and page["next_offset"] is None


# --- authorization snapshot ----------------------------------------------


def test_checkpoint_grants_win_over_the_current_revision(app, agent_id, tmp_path):
    conversation = app.conversations.create(agent_id, "撤回")
    run_id = make_run(app, agent_id, conversation["id"])
    folder = tmp_path / "authorized"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
    RunCheckpoints(app.database).save(run_id, {"grants": app.permissions.current(agent_id)})
    old = app.permissions.current(agent_id)
    # The owner withdraws the grant; the capture keeps its admission snapshot.
    app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=1)
    stored = EventRecordStore(app).record(run_id, "tool_result", "旧输出", step=4)
    assert stored["grants"]["captured_from"] == "checkpoint"
    assert stored["grants"]["permissions"] == old
    assert stored["grants"]["permissions"]["revision"] == 1
    assert EventRecordStore(app).read(stored["id"])["grants"] == stored["grants"]


def test_runtime_scope_snapshot_wins_and_current_is_the_last_resort(app, agent_id, tmp_path):
    conversation = app.conversations.create(agent_id, "快照")
    run_id = make_run(app, agent_id, conversation["id"])
    store = EventRecordStore(app)
    fallback = store.record(run_id, "tool_call", "{}", step=1, call_id="c1")
    assert fallback["grants"]["captured_from"] == "current"
    assert fallback["grants"]["permissions"]["revision"] == 0
    snapshot = {"agent_id": agent_id, "revision": 7, "readable": ["/tmp/a"], "writable": []}
    trusted = store.record(
        run_id,
        "tool_call",
        "{}",
        step=1,
        call_id="c2",
        metadata={"name": "read_file", "scope_grants": snapshot},
    )
    assert trusted["grants"]["captured_from"] == "runtime"
    assert trusted["grants"]["permissions"] == snapshot
    assert trusted["grants"]["browser"] is None
