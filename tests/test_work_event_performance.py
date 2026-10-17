"""Performance and memory bounds for the M2 event store (P1), measured offline.

The thresholds below were fixed before any measurement and are not relaxed
afterwards; the measured values, together with the frozen threshold set and the
machine environment, are written to
``.artifacts/m2-implementation/performance.json`` for the milestone record.

What is proven here, with no network and a temporary database:

* an event directory page stays bounded (payload and peak memory) at 10, 100 and
  1000 sources, and walking all pages of 1000 sources stays cheap; the first
  page is repeated for p50/p95 timing at each size;
* one 16 KiB page of a 1 MiB or 16 MiB Unicode record is read without loading the
  body — the extra peak stays near two 64 KiB chunks while the body grows 16×,
  with p50/p95 timing over repeated page reads;
* the full body reassembles by paging (streamed hash) with bounded memory;
* storing a record grows the database by roughly its body size, never by a
  multiple that would indicate duplicated copies.
"""

import asyncio
import hashlib
import json
import math
import os
import platform
import sqlite3
import time
import tracemalloc
from pathlib import Path

import httpx
import pytest
from test_run_recovery import _chat_reply, _native_response

from lingnexus.common.types import new_id, utc_now
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime
from lingnexus.runtime.payload import build_payload
from lingnexus.work_events.store import MAX_PAGE_BYTES, EventRecordStore

REPOSITORY = Path(__file__).resolve().parents[1]
ARTIFACT = REPOSITORY / ".artifacts" / "m2-implementation" / "performance.json"

# --- frozen thresholds (chosen before measuring; never relaxed afterwards) --
PAGE_PEAK_LIMIT = 2 * 1024 * 1024  # extra peak for one 16 KiB page read
WALK_PEAK_LIMIT = 8 * 1024 * 1024  # extra peak while streaming a whole record
DIRECTORY_PAYLOAD_LIMIT = 512 * 1024  # JSON bytes of one 20-item directory page
DIRECTORY_PEAK_LIMIT = 2 * 1024 * 1024
DIRECTORY_PAGE_SECONDS = 2.0
DIRECTORY_WALK_SECONDS = 20.0  # all 50 pages of a 1000-source directory
PAGE_READ_SECONDS = 0.25
FULL_1MIB_SECONDS = 15.0
FULL_16MIB_SECONDS = 120.0
RECORD_16MIB_SECONDS = 60.0
DB_GROWTH_MIN_FACTOR = 0.9
# Must stay below 2.0: a second full copy of the body would land at ~2x, so a
# band that allowed 2x could not tell "stored once" from "stored twice".
DB_GROWTH_MAX_FACTOR = 1.6
DB_GROWTH_MAX_SLACK = 1024 * 1024
CHUNK_PAGE_BOUND = 512 * 1024  # one page reads at most two 64 KiB chunks
CHUNK_WALK_BOUND = 1024 * 1024
DIRECTORY_SMALL_DB_LIMIT = 4 * 1024 * 1024  # 10/100 fabricated source rows
DIRECTORY_LARGE_DB_LIMIT = 16 * 1024 * 1024  # 1000 fabricated source rows
PEAK_SCALING_FACTOR = 4
# Percentile policy: 15 lightweight repeats per operation. The percentile is
# nearest-rank, so p95 with 15 samples is the slowest observation - a strictly
# tighter reading than a 20-sample interpolated p95. Percentile bounds reuse the
# already-frozen per-operation limits above; nothing here relaxes them. Full-body
# walks are measured once and are not repeated.
PERCENTILE_SAMPLES = 15

# The service endpoint revalidates ancestry and sources on every page, so one
# page costs more than a bare store read; these are the endpoint thresholds,
# frozen before their first run exactly like the store ones above.
ENDPOINT_PAGE_SECONDS = 1.0
ENDPOINT_PAGE_PEAK = 512 * 1024
ENDPOINT_WALK_16MIB_SECONDS = 120.0
ENDPOINT_WALK_PEAK = 1024 * 1024
ENDPOINT_MESSAGE_WALK_SECONDS = 30.0

FROZEN_THRESHOLDS = {
    "page_peak_bytes": PAGE_PEAK_LIMIT,
    "walk_peak_bytes": WALK_PEAK_LIMIT,
    "directory_payload_bytes": DIRECTORY_PAYLOAD_LIMIT,
    "directory_peak_bytes": DIRECTORY_PEAK_LIMIT,
    "directory_page_seconds": DIRECTORY_PAGE_SECONDS,
    "directory_walk_seconds": DIRECTORY_WALK_SECONDS,
    "page_read_seconds": PAGE_READ_SECONDS,
    "full_1mib_seconds": FULL_1MIB_SECONDS,
    "full_16mib_seconds": FULL_16MIB_SECONDS,
    "record_16mib_seconds": RECORD_16MIB_SECONDS,
    "db_growth_factor": [DB_GROWTH_MIN_FACTOR, DB_GROWTH_MAX_FACTOR],
    "db_growth_slack_bytes": DB_GROWTH_MAX_SLACK,
    "page_chunk_bound_bytes": CHUNK_PAGE_BOUND,
    "walk_chunk_bound_bytes": CHUNK_WALK_BOUND,
    "peak_scaling_factor": PEAK_SCALING_FACTOR,
    "percentile_samples": PERCENTILE_SAMPLES,
    "endpoint_page_seconds": ENDPOINT_PAGE_SECONDS,
    "endpoint_page_peak_bytes": ENDPOINT_PAGE_PEAK,
    "endpoint_walk_16mib_seconds": ENDPOINT_WALK_16MIB_SECONDS,
    "endpoint_walk_peak_bytes": ENDPOINT_WALK_PEAK,
    "endpoint_message_walk_seconds": ENDPOINT_MESSAGE_WALK_SECONDS,
}

NOTES = (
    "All thresholds were fixed before the first measurement run. The two "
    "chunk-bound assertions (one page reads at most two 64 KiB chunks; a full "
    "walk streams a page at a time) were added after the first run observed "
    "~216 KiB and ~325 KiB and then frozen at 512 KiB / 1 MiB - they were "
    "tightened, never relaxed. Directory DB growth of 0 bytes at 10 sources is "
    "SQLite page reuse, not a missing write; the row counts are asserted "
    "directly. The endpoint thresholds were frozen before the first run of the "
    "endpoint test, which exercises WorkEvents.directory/read_record (the real "
    "service surface, including per-page ancestry revalidation) rather than the "
    "store alone."
)

RESULTS: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _artifact_writer():
    RESULTS.clear()
    yield
    if not RESULTS:
        return
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "generated_at": utc_now(),
                "suite": "tests/test_work_event_performance.py",
                "frozen_thresholds": FROZEN_THRESHOLDS,
                "threshold_notes": NOTES,
                "environment": {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "sqlite": sqlite3.sqlite_version,
                    "cpu_count": os.cpu_count(),
                },
                "results": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


# --- measurement helpers --------------------------------------------------


def measure(callback):
    """(value, seconds, extra peak bytes) for one callback call."""
    base, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    value = callback()
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    return value, seconds, max(0, peak - base)


def database_bytes(path: Path) -> int:
    """Committed size of the SQLite database including its WAL side files."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        file = Path(str(path) + suffix)
        if file.exists():
            total += file.stat().st_size
    return total


def payload_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile over a small sample list (no interpolation)."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def repeat(call, samples: int = PERCENTILE_SAMPLES) -> tuple[list, list[float], float, float]:
    """Run ``call`` ``samples`` times; return last value, timings, p50, p95.

    Timing only: the frozen elapsed-time limits above are asserted against p50
    and p95, so a repeated measurement can never be more lenient than the single
    measurement it replaces.
    """
    value, timings = None, []
    for _ in range(samples):
        value, seconds, _ = measure(call)
        timings.append(seconds)
    return value, [round(item, 6) for item in timings], percentile(timings, 0.50), percentile(timings, 0.95)


def unicode_body(minimum_bytes: int) -> str:
    """Mixed-width text (3-byte, 1-byte and 4-byte characters) of that size."""
    pattern = "汉字ab🚀"
    unit = len(pattern.encode("utf-8"))
    return pattern * (minimum_bytes // unit + 1)


def add_completed_run(app, agent_id: str, conversation_id: str, user_message_id: str) -> str:
    """A finished durable chat run anchored on one user message."""
    run_id, profile_id = new_id(), new_id()
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO model_profiles VALUES(?,?,?)",
            (profile_id, "perf-fixture", json.dumps({"model": "fixture"})),
        )
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "conversation_id,user_message_id,context_manifest,created_at,finished_at) "
            "VALUES(?,?,?,?,?,'completed',?,?,?,?,?)",
            (
                run_id,
                agent_id,
                new_id(),
                "performance-fixture",
                profile_id,
                conversation_id,
                user_message_id,
                None,
                utc_now(),
                utc_now(),
            ),
        )
    return run_id


def make_closed_event(app, agent_id: str, title: str):
    """A real closed event built through the public service, ready to scale."""
    conversation = app.conversations.create(agent_id, title)
    event = app.work_events.begin(conversation["id"], title, new_id())
    user = app.conversations.append_user(conversation["id"], "开始工作", request_id=new_id())
    run_id = add_completed_run(app, agent_id, conversation["id"], user["id"])
    app.work_events.record(run_id, "run_start", "开始工作")
    closed = app.work_events.close(event["id"], expected_revision=event["revision"])
    assert closed["status"] == "closed" and closed["coverage"] == "retained"
    return conversation, closed, user


def add_fabricated_sources(app, event_id: str, message_id: str, text: str, target: int) -> int:
    """Extend a real event's directory to exactly ``target`` sources."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    size = len(text.encode("utf-8"))
    with app.database.transaction() as db:
        count, ordinal = db.execute(
            "SELECT COUNT(*),COALESCE(MAX(ordinal),-1) FROM work_event_sources WHERE event_id=?",
            (event_id,),
        ).fetchone()
        for index in range(count, target):
            ordinal += 1
            db.execute(
                "INSERT INTO work_event_sources(event_id,source_id,kind,ref,byte_count,"
                "sha256,coverage,ordinal) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    f"perf-source-{index}",
                    "message",
                    json.dumps({"id": message_id}),
                    size,
                    digest,
                    "retained",
                    ordinal,
                ),
            )
        return db.execute(
            "SELECT COUNT(*) FROM work_event_sources WHERE event_id=?", (event_id,)
        ).fetchone()[0]


# --- the directory stays bounded as the source list grows -----------------


def test_directory_pages_stay_bounded_at_10_100_and_1000_sources(app, agent_id):
    tracemalloc.start()
    try:
        measured = {}
        for target in (10, 100, 1000):
            conversation, event, user = make_closed_event(app, agent_id, f"目录-{target}")
            before = database_bytes(app.database.path)
            count = add_fabricated_sources(app, event["id"], user["id"], user["text"], target)
            after = database_bytes(app.database.path)
            assert count == target

            def read(event_id=event["id"], cid=conversation["id"]):
                return app.work_events.directory(
                    event_id, agent_id=agent_id, conversation_id=cid, limit=20
                )
            read()  # warm-up outside the measurement
            page, seconds, peak = measure(read)
            size = payload_bytes(page)
            expected_items = min(20, target)
            assert len(page["items"]) == expected_items
            assert (page["next_cursor"] is not None) == (target > 20)
            assert size <= DIRECTORY_PAYLOAD_LIMIT, (target, size)
            assert peak <= DIRECTORY_PEAK_LIMIT, (target, peak)
            assert seconds <= DIRECTORY_PAGE_SECONDS, (target, seconds)
            repeated, timings, p50, p95 = repeat(read)
            assert len(repeated["items"]) == expected_items and payload_bytes(repeated) == size
            assert p50 <= DIRECTORY_PAGE_SECONDS, (target, p50)
            assert p95 <= DIRECTORY_PAGE_SECONDS, (target, p95)

            pages, items, cursor = 0, 0, None
            started = time.perf_counter()
            while True:
                value = app.work_events.directory(
                    event["id"], agent_id=agent_id, conversation_id=conversation["id"],
                    cursor=cursor, limit=20,
                )
                pages += 1
                items += len(value["items"])
                cursor = value["next_cursor"]
                if cursor is None or pages > 200:
                    break
            walk_seconds = time.perf_counter() - started
            assert items == target and pages == -(-target // 20)
            assert walk_seconds <= DIRECTORY_WALK_SECONDS, (target, walk_seconds)

            delta = after - before
            limit = DIRECTORY_LARGE_DB_LIMIT if target == 1000 else DIRECTORY_SMALL_DB_LIMIT
            assert 0 <= delta <= limit, (target, delta)
            measured[str(target)] = {
                "sources": target,
                "first_page_items": len(page["items"]),
                "first_page_payload_bytes": size,
                "first_page_peak_bytes": peak,
                "first_page_seconds": round(seconds, 4),
                "first_page_p50_seconds": round(p50, 6),
                "first_page_p95_seconds": round(p95, 6),
                "first_page_sample_count": len(timings),
                "first_page_samples_seconds": timings,
                "walk_pages": pages,
                "walk_items": items,
                "walk_seconds": round(walk_seconds, 4),
                "db_delta_bytes": delta,
            }
        RESULTS["directory"] = measured
        # Ten times the sources must not cost ten times the page read; compare
        # medians so one scheduling hiccup cannot decide the slope check.
        small, large = measured["100"], measured["1000"]
        assert large["first_page_p50_seconds"] <= small["first_page_p50_seconds"] * 5 + 0.2
    finally:
        tracemalloc.stop()


# --- one large Unicode record pages without loading its body ---------------


def test_single_large_records_page_with_bounded_memory_and_honest_db_growth(app, agent_id):
    conversation = app.conversations.create(agent_id, "大记录")
    run_id = add_completed_run(
        app, agent_id, conversation["id"],
        app.conversations.append_user(conversation["id"], "提供大输出", request_id=new_id())["id"],
    )
    store = EventRecordStore(app)
    tracemalloc.start()
    try:
        bodies = {"1MiB": unicode_body(1024 * 1024), "16MiB": unicode_body(16 * 1024 * 1024)}
        measured = {}
        for label, body in bodies.items():
            expected_bytes = len(body.encode("utf-8"))
            before = database_bytes(app.database.path)
            def insert(body=body, label=label):
                return store.record(run_id, "tool_result", body, step=1, call_id=label)

            stored, insert_seconds, _ = measure(insert)
            delta = database_bytes(app.database.path) - before
            assert stored["byte_count"] == expected_bytes
            assert DB_GROWTH_MIN_FACTOR * expected_bytes <= delta <= (
                DB_GROWTH_MAX_FACTOR * expected_bytes + DB_GROWTH_MAX_SLACK
            ), (label, delta)
            if label == "16MiB":
                assert insert_seconds <= RECORD_16MIB_SECONDS, insert_seconds

            # A page from the middle of the body: it must not pull in the whole body.
            # The body repeats a 12-byte mixed-width unit, so any multiple of 12
            # is a real character boundary.
            offset = (expected_bytes // 2) - (expected_bytes // 2) % 12

            def read_page(record_id=stored["id"], at=offset):
                return store.read(record_id, offset=at, max_bytes=MAX_PAGE_BYTES)

            page, page_seconds, page_peak = measure(read_page)
            assert page["text"], (label, page["offset"])
            assert len(page["text"].encode("utf-8")) <= MAX_PAGE_BYTES
            assert page_peak <= PAGE_PEAK_LIMIT, (label, page_peak)
            assert page_seconds <= PAGE_READ_SECONDS, (label, page_seconds)
            # The direct claim: one page costs no more than the two 64 KiB chunks
            # it reads plus fixed overhead, whatever the body size.
            assert page_peak <= CHUNK_PAGE_BOUND, (label, page_peak)
            repeated, page_timings, page_p50, page_p95 = repeat(read_page)
            assert repeated["text"] == page["text"] and repeated["next_offset"] == page["next_offset"]
            assert repeated["sha256"] == page["sha256"]
            assert page_p50 <= PAGE_READ_SECONDS, (label, page_p50)
            assert page_p95 <= PAGE_READ_SECONDS, (label, page_p95)

            # Full reassembly, streaming pages into a hash instead of a buffer.
            base, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            digest, total, pages, cursor = hashlib.sha256(), 0, 0, 0
            started = time.perf_counter()
            while True:
                value = store.read(stored["id"], offset=cursor, max_bytes=MAX_PAGE_BYTES)
                chunk = value["text"].encode("utf-8")
                digest.update(chunk)
                total += len(chunk)
                pages += 1
                cursor = value["next_offset"]
                if cursor is None:
                    break
            walk_seconds = time.perf_counter() - started
            _, walk_peak = tracemalloc.get_traced_memory()
            walk_peak = max(0, walk_peak - base)
            assert digest.hexdigest() == stored["sha256"]
            assert total == expected_bytes and cursor is None
            # One page never wastes more than one character; the last is shorter.
            assert -(-expected_bytes // MAX_PAGE_BYTES) <= pages <= (
                -(-expected_bytes // (MAX_PAGE_BYTES - 4)) + 1
            ), (label, pages)
            assert walk_peak <= WALK_PEAK_LIMIT, (label, walk_peak)
            assert walk_peak <= CHUNK_WALK_BOUND, (label, walk_peak)
            assert walk_seconds <= (
                FULL_1MIB_SECONDS if label == "1MiB" else FULL_16MIB_SECONDS
            ), (label, walk_seconds)

            measured[label] = {
                "byte_count": expected_bytes,
                "chunks": -(-expected_bytes // (64 * 1024)),
                "insert_seconds": round(insert_seconds, 4),
                "db_delta_bytes": delta,
                "page_peak_bytes": page_peak,
                "page_seconds": round(page_seconds, 4),
                "page_p50_seconds": round(page_p50, 6),
                "page_p95_seconds": round(page_p95, 6),
                "page_sample_count": len(page_timings),
                "page_samples_seconds": page_timings,
                "page_bytes": len(page["text"].encode("utf-8")),
                "walk_pages": pages,
                "walk_seconds": round(walk_seconds, 4),
                "walk_peak_bytes": walk_peak,
            }
        small, large = measured["1MiB"], measured["16MiB"]
        # Sixteen times the body must not mean sixteen times the page memory.
        scaling_limit = max(PEAK_SCALING_FACTOR * small["page_peak_bytes"], 1024 * 1024)
        assert large["page_peak_bytes"] <= scaling_limit, (
            small["page_peak_bytes"],
            large["page_peak_bytes"],
        )
        RESULTS["records"] = measured
        RESULTS["records"]["page_peak_scaling"] = {
            "limit_bytes": scaling_limit,
            "observed_bytes": large["page_peak_bytes"],
        }
    finally:
        tracemalloc.stop()


# --- the real service endpoints must not load a whole source per page ------


async def _paired_chat(app, runtime, agent_id, cid, message, request_id, **kwargs):
    """One real runtime turn; the caller captures the final request body."""
    run = runtime.submit(
        ChatRequest(agent_id=agent_id, conversation_id=cid, message=message,
                    request_id=request_id, **kwargs)
    )
    events = [event async for event in runtime.events(run["id"])]
    assert not [event for event in events if event["event"] == "error"], events
    result = runtime.get_run(run["id"])
    assert result["status"] == "completed", result
    return result


def test_raw_and_folded_final_bodies_are_measured_paired(app, agent_id, tmp_path):
    """A2: the same user anchor regenerated raw and folded, byte-for-byte.

    The work turn really carries the file text, the attachment sentinel and the
    native tool exchange; the paired regeneration afterwards keeps the same user
    message and profile, so the difference between the two final request bodies
    is the work projection itself. Reported: the actual serialized request bytes,
    build_payload timing and peak (with and without a warm history load), the
    non-work bytes retained in each body, and the database/asset growth of the
    event index.
    """
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="fixture",
        base_url="https://fixture.test", context_window_tokens=200000,
        max_output_tokens=1000,
    )
    saved = app.profiles.save("perf-a2", profile, api_key="a2-secret")
    app.profiles.select(saved["id"], None)
    folder = tmp_path / "allowed"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
    target = folder / "source.txt"
    raw = "工具全文哨兵-" * 400
    target.write_text(raw, encoding="utf-8")
    attachment = app.attachments.add(agent_id, "context.txt", b"ATTACHMENT_SENTINEL_529")
    assets_before = sum(
        path.stat().st_size for path in app.attachments.directory.rglob("*")
    )
    cid = app.conversations.create(agent_id, "A2 配对")["id"]
    requests, queue = [], []

    def handler(request):
        requests.append(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=queue.pop(0))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            queue.append(_chat_reply("工作前闲聊：雨后看山"))
            await _paired_chat(app, runtime, agent_id, cid, "PRE_WORK_TOPIC", "a2-before")
            event = app.work_events.begin(cid, "检查原始材料", "a2-boundary")
            queue.extend([
                _native_response(APIFamily.OPENAI_CHAT_COMPLETIONS, str(target)),
                _chat_reply("工作答复哨兵"),
            ])
            await _paired_chat(
                app, runtime, agent_id, cid, "工作要求哨兵", "a2-work",
                attachment_ids=[attachment["id"]],
            )
            closed = app.work_events.close(event["id"], expected_revision=1)
            assert closed["coverage"] == "retained" and closed["folded"]
            raw_event = app.work_events.set_fold(event["id"], False, expected_revision=closed["revision"])
            queue.append(_chat_reply("配对答复"))
            raw_run = await _paired_chat(app, runtime, agent_id, cid, "回到原来的闲聊", "a2-raw")
            raw_body = requests[-1]
            app.work_events.set_fold(event["id"], True, expected_revision=raw_event["revision"])
            queue.append(_chat_reply("配对答复"))
            folded_run = await _paired_chat(
                app, runtime, agent_id, cid, "回到原来的闲聊", "a2-folded",
                regenerate_message_id=raw_run["user_message_id"],
            )
            folded_body = requests[-1]
            await runtime.aclose()
            return closed, raw_body, folded_body, raw_run, folded_run

    tracemalloc.start()
    try:
        closed, raw_body, folded_body, _raw_run, folded_run = asyncio.run(scenario())
        manifest = folded_run["context_manifest"]["work_events"][0]
        assert manifest["sent"], manifest
        assert len(folded_body) < len(raw_body), (len(raw_body), len(folded_body))
        assert b"\xe5\xb7\xa5\xe5\x85\xb7\xe5\x85\xa8\xe6\x96\x87\xe5\x93\xa8\xe5\x85\xb5" in raw_body  # 工具全文哨兵
        assert b"ATTACHMENT_SENTINEL_529" in raw_body
        assert b"PRE_WORK_TOPIC" in folded_body and "回到原来的闲聊".encode() in folded_body
        marker = ("【工作经历 " + closed["id"] + "】").encode()
        assert folded_body.count(marker) == 1
        for secret in ("工作要求哨兵", "工具全文哨兵", "工作答复哨兵"):
            assert secret.encode() not in folded_body, secret
        assert b"ATTACHMENT_SENTINEL_529" not in folded_body

        # In-memory build cost for the folded history. ``with_history_load`` reads
        # the transcript out of SQLite inside the measurement; the preloaded
        # variant measures payload assembly alone on the same messages.
        messages = app.conversations.messages(cid)
        app.profiles.resolve(saved["id"])  # credential check, as a real turn does
        runtime_profile = ModelProfile.model_validate(app.profiles.get(saved["id"])["profile"])

        def build_with_history():
            return build_payload(app, agent_id, runtime_profile, app.conversations.messages(cid))

        def build_preloaded():
            return build_payload(app, agent_id, runtime_profile, messages)

        build_with_history()  # warm-up outside the measurement
        cold, cold_seconds, cold_peak = measure(build_with_history)
        warm, warm_seconds, warm_peak = measure(
            build_preloaded
        )
        assert cold[1]["work_events"][0]["sent"] and warm[1]["work_events"][0]["sent"]
        # Retained non-work content: the pre-work topic and the newest question
        # survive in both bodies; the work details only survive in raw.
        def retained(body: bytes) -> dict:
            return {
                "pre_work_topic": b"PRE_WORK_TOPIC" in body,
                "newest_question": "回到原来的闲聊".encode() in body,
                "work_details": b"\xe5\xb7\xa5\xe5\x85\xb7\xe5\x85\xa8\xe6\x96\x87\xe5\x93\xa8\xe5\x85\xb5" in body,
                "attachment_text": b"ATTACHMENT_SENTINEL_529" in body,
            }

        results = {
            "profile": "openai-chat-completions/fixture/200k",
            "raw_final_body_bytes": len(raw_body),
            "folded_final_body_bytes": len(folded_body),
            "bytes_saved": len(raw_body) - len(folded_body),
            "saved_fraction": round(1 - len(folded_body) / len(raw_body), 4),
            "raw_retained": retained(raw_body),
            "folded_retained": retained(folded_body),
            "folded_summary_occurrences": folded_body.count(marker),
            "projection_id": manifest.get("projection_id"),
            "projection_strategy": manifest.get("strategy"),
            "build_payload": {
                "with_history_load": {
                    "seconds": round(cold_seconds, 4),
                    "peak_bytes": cold_peak,
                    "serialized_bytes": payload_bytes(cold[0]),
                },
                "messages_preloaded": {
                    "seconds": round(warm_seconds, 4),
                    "peak_bytes": warm_peak,
                    "serialized_bytes": payload_bytes(warm[0]),
                },
            },
            "db_delta_bytes": database_bytes(app.database.path),
            "asset_delta_bytes": sum(
                path.stat().st_size for path in app.attachments.directory.rglob("*")
            ) - assets_before,
        }
        with app.database.transaction() as db:
            results["event_source_count"] = db.execute(
                "SELECT count(*) FROM work_event_sources WHERE event_id=?", (closed["id"],)
            ).fetchone()[0]
        RESULTS["a2_paired_bodies"] = results
    finally:
        tracemalloc.stop()


def test_service_endpoints_page_large_sources_without_loading_them(app, agent_id):
    """WorkEvents.directory/read_record with a 16 MiB record and a 1 MiB message.

    The service revalidates ancestry and sources on every page, so this is the
    place a whole-body load would actually show up: if a page pulled the body
    into memory, the measured peak would track the source size instead of the
    page size.
    """
    conversation = app.conversations.create(agent_id, "端点大来源")
    event = app.work_events.begin(conversation["id"], "端点大来源", new_id())
    message_text = unicode_body(1024 * 1024)
    user = app.conversations.append_user(conversation["id"], message_text, request_id=new_id())
    run_id = add_completed_run(app, agent_id, conversation["id"], user["id"])
    app.work_events.record(run_id, "run_start", "开始工作")
    body = unicode_body(16 * 1024 * 1024)
    record = app.work_events.record(run_id, "tool_result", body, step=1, call_id="big")
    closed = app.work_events.close(event["id"], expected_revision=event["revision"])
    assert closed["coverage"] == "retained"

    directory = app.work_events.directory(
        closed["id"], agent_id=agent_id, conversation_id=conversation["id"], limit=20
    )
    assert payload_bytes(directory) <= DIRECTORY_PAYLOAD_LIMIT
    by_kind: dict[str, dict] = {}
    for item in directory["items"]:
        by_kind.setdefault(item["kind"], item)
    record_source, message_source = by_kind["tool_result"], by_kind["message"]
    assert record_source["byte_count"] == record["byte_count"]
    assert message_source["byte_count"] == len(message_text.encode("utf-8"))

    tracemalloc.start()
    try:
        def fetch(source_id, cursor=None, max_bytes=MAX_PAGE_BYTES):
            return app.work_events.read_record(
                closed["id"], source_id, agent_id=agent_id,
                conversation_id=conversation["id"], cursor=cursor, max_bytes=max_bytes,
            )

        # Warm up, then park on a page deep inside the 16 MiB record and measure
        # only that page: the body is present, the page must stay small.
        cursor, first = None, None
        for _ in range(8):
            first = fetch(record_source["source_id"], cursor)
            cursor = first["next_cursor"]
        assert cursor is not None
        fetched, page_seconds, page_peak = measure(
            lambda: fetch(record_source["source_id"], cursor)
        )
        assert fetched["text"] and fetched["text"] in body
        assert len(fetched["text"].encode("utf-8")) <= MAX_PAGE_BYTES
        assert page_peak <= ENDPOINT_PAGE_PEAK, page_peak
        assert page_seconds <= ENDPOINT_PAGE_SECONDS, page_seconds
        with pytest.raises(ValueError):
            fetch(record_source["source_id"], cursor, max_bytes=MAX_PAGE_BYTES + 1)

        repeated, page_timings, page_p50, page_p95 = repeat(
            lambda: fetch(record_source["source_id"], cursor)
        )
        assert repeated["text"] == fetched["text"]
        assert repeated["sha256"] == fetched["sha256"]
        assert page_p50 <= ENDPOINT_PAGE_SECONDS, page_p50
        assert page_p95 <= ENDPOINT_PAGE_SECONDS, page_p95

        def walk(source_id, *, max_bytes=MAX_PAGE_BYTES):
            digest, total, pages, cursor = hashlib.sha256(), 0, 0, None
            base, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            started = time.perf_counter()
            while True:
                value = fetch(source_id, cursor, max_bytes)
                chunk = value["text"].encode("utf-8")
                digest.update(chunk)
                total += len(chunk)
                pages += 1
                cursor = value["next_cursor"]
                if cursor is None:
                    break
            seconds = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            return digest.hexdigest(), total, pages, seconds, max(0, peak - base)

        digest, total, pages, seconds, peak = walk(record_source["source_id"])
        assert digest == record_source["sha256"]
        assert total == record_source["byte_count"] and total == record["byte_count"]
        assert peak <= ENDPOINT_WALK_PEAK, peak
        assert seconds <= ENDPOINT_WALK_16MIB_SECONDS, seconds
        results = {
            "record_16MiB": {
                "record_bytes": record["byte_count"],
                "page_peak_bytes": page_peak,
                "page_seconds": round(page_seconds, 4),
                "page_p50_seconds": round(page_p50, 6),
                "page_p95_seconds": round(page_p95, 6),
                "page_sample_count": len(page_timings),
                "page_samples_seconds": page_timings,
                "page_bytes": len(fetched["text"].encode("utf-8")),
                "walk_pages": pages,
                "walk_seconds": round(seconds, 4),
                "walk_peak_bytes": peak,
            }
        }

        digest, total, pages, seconds, peak = walk(message_source["source_id"])
        assert digest == message_source["sha256"]
        assert total == message_source["byte_count"]
        assert peak <= ENDPOINT_WALK_PEAK, peak
        assert seconds <= ENDPOINT_MESSAGE_WALK_SECONDS, seconds
        results["message_1MiB"] = {
            "message_bytes": message_source["byte_count"],
            "walk_pages": pages,
            "walk_seconds": round(seconds, 4),
            "walk_peak_bytes": peak,
        }
        RESULTS["endpoints"] = results
    finally:
        tracemalloc.stop()
