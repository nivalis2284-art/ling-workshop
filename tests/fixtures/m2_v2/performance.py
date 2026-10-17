"""Offline N16 measurement entry point; never opens a real model transport.

Run from the repository root:
  .venv/Scripts/python.exe tests/fixtures/m2_v2/performance.py --samples 30
JSON includes raw samples and frozen limits. Missing interfaces are pending,
not zero timings. All databases/assets live under TemporaryDirectory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path

import httpx

from lingnexus.app import Application
from lingnexus.models import APIFamily
from lingnexus.runtime import ChatRequest, ChatRuntime

try:
    from .protocols import MockModelTransport, profile, response_for
except ImportError:  # direct script invocation
    from protocols import MockModelTransport, profile, response_for


LIMITS = {
    "admission_p95_ms_floor": 250, "admission_baseline_multiplier": 2,
    "admission_max_ms": 1000, "additional_loop_lag_p95_ms": 100,
    "additional_loop_lag_max_ms": 500, "publish_lock_p95_ms": 100,
    "publish_lock_max_ms": 500, "candidate_max_bytes": 131072,
}


def summary(values):
    if not values:
        return {"status": "pending", "samples": []}
    ordered = sorted(values)
    return {"n": len(values), "p50": statistics.median(values),
            "p95": ordered[math.ceil(len(values) * .95) - 1],
            "max": max(values), "samples": values}


def measure(call, samples):
    call()  # warmup is excluded
    values = []
    for _ in range(samples):
        start = time.perf_counter()
        call()
        values.append((time.perf_counter() - start) * 1000)
    return summary(values)


@contextmanager
def transaction_probe(database, values):
    """Record outer write-transaction scope including commit, no nested double count.

    Start timing after BEGIN IMMEDIATE succeeds. This captures the Python lock
    hold / SQL write interval, not time spent waiting to acquire the DB lock.
    The wrapper is installed only while publish executes.
    """
    original = database.transaction
    depth = 0

    @contextmanager
    def timed():
        nonlocal depth
        with original() as db:
            outer = depth == 0
            depth += 1
            start = time.perf_counter()
            try:
                yield db
            finally:
                depth -= 1
                if outer:
                    values.append((time.perf_counter() - start) * 1000)

    database.transaction = timed
    try:
        yield
    finally:
        database.transaction = original


def seed_experiences(app, aid, cid, count):
    with app.database.transaction() as db:
        db.executemany(
            "INSERT INTO continuity_experiences(id,agent_id,kind,title,summary,revision,"
            "conversation_id,created_at,updated_at) VALUES(?,?,'main_event',?,?,1,?,?,?)",
            [(f"bench-{i}", aid, f"事件{i}", "合成摘要", cid,
              "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
             for i in range(count)],
        )


async def admissions(app, aid, cid, samples, *, background=None):
    transport = MockModelTransport.from_responses(
        [response_for(APIFamily.OPENAI_CHAT_COMPLETIONS, "离线答复")] * (samples + 1)
    )
    metrics, lag = [], []
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            start = asyncio.get_running_loop().time()
            await asyncio.sleep(.01)
            lag.append(max(0, (asyncio.get_running_loop().time() - start - .01) * 1000))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as client:
        runtime = ChatRuntime(app, client=client)
        ticker_task = asyncio.create_task(ticker())
        background_task = asyncio.create_task(background()) if background else None
        try:
            for i in range(samples + 1):
                start = time.perf_counter()
                run = runtime.submit(ChatRequest(agent_id=aid, conversation_id=cid,
                                     message="性能探针", request_id=f"perf-{time.time_ns()}"))
                elapsed = (time.perf_counter() - start) * 1000
                if i:
                    metrics.append(elapsed)
                async for _ in runtime.events(run["id"]):
                    pass
                assert runtime.get_run(run["id"])["status"] == "completed"
            if background_task:
                await asyncio.wait_for(background_task, 30)
        finally:
            stop.set()
            await ticker_task
            if background_task and not background_task.done():
                background_task.cancel()
                await asyncio.gather(background_task, return_exceptions=True)
            await runtime.aclose()
    return {"admission_ms": summary(metrics), "loop_lag_ms": summary(lag),
            "final_http_body_bytes": [len(c["body"]) for c in transport.calls]}


def run(samples=30):
    if samples < 30:
        raise ValueError("frozen acceptance requires at least 30 samples")
    report = {"limits": LIMITS, "samples": samples, "directory": {}, "source": {},
              "publish_lock_ms": {"status": "pending: publication integration required"},
              "concurrent_admission": {"status": "pending: runner integration required"},
              "hash_inside_publish": "requires separate fault/code evidence; timing cannot prove this"}
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="ling-m2-perf-") as folder:
        root = Path(folder)
        for count in (10, 100, 1000):
            app = Application(root / str(count) / "ling.db")
            try:
                from lingnexus.continuity import Continuity

                if not getattr(app, "continuity", None):
                    app.continuity = Continuity(app)
                aid = app.create_agent("合成性能样本")
                cid = app.conversations.create(aid, "性能主干")["id"]
                app.continuity.main.bind(aid, cid, 0)
                seed_experiences(app, aid, cid, count)
                report["directory"][count] = measure(
                    lambda a=app, agent=aid: a.continuity.experiences.list(agent), samples
                )
                if count == 10:
                    for mib in (1, 16):
                        text = "x" * (mib * 1048576)
                        message = app.conversations.append_user(cid, text, request_id=f"source-{mib}")
                        app.continuity.experiences.add_source(
                            "bench-0", f"large-{mib}", "message", {"id": message["id"]}, mib, text
                        )
                        report["source"][f"{mib}MiB_first_page_ms"] = measure(
                            lambda size=mib, a=app, agent=aid, conversation=cid: (
                                a.continuity.experiences.read(
                                    "bench-0", f"large-{size}", agent_id=agent,
                                    conversation_id=conversation, max_bytes=8192
                                )
                            ), samples
                        )
                    item = app.profiles.save("offline", profile(APIFamily.OPENAI_CHAT_COMPLETIONS),
                                             api_key="synthetic-key")
                    app.profiles.select(item["id"], None)
                    report["baseline"] = asyncio.run(admissions(app, aid, cid, samples))
                report.setdefault("database_bytes", {})[count] = sum(
                    p.stat().st_size for p in app.database.path.parent.glob("ling.db*")
                )
            finally:
                app.close()
        current, peak = tracemalloc.get_traced_memory()
        report["python_memory_bytes"] = {"current": current, "peak": peak}
    tracemalloc.stop()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(run(args.samples), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result)
