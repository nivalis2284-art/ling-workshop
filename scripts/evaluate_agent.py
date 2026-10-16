"""Small general-task acceptance probes; dry-run by default, shared paid ledger.

These probes cover three categories, not the complete five-category M1 contract.
No retry or model switching; the first infrastructure failure stops live trials.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from evaluate_coding import (
    DEFAULT_BASE_URL,
    DEFAULT_BUDGET,
    DEFAULT_BUDGET_USD,
    DEFAULT_MODEL,
    INPUT_RATE,
    OUTPUT_RATE,
    BudgetFile,
    BudgetTransport,
    _source_identity,
)

CASES = ("information", "documents", "tools")


def accept(case, root, final, tool_names):
    """Separate business acceptance from the runtime's verification flag."""
    try:
        if case == "information":
            value = json.loads(final.strip())
            return value == {"owner": "Lin", "city": "Tokyo", "items": 3}
        if case == "documents":
            text = (root / "summary.txt").read_text(encoding="utf-8")
            return ("Tuesday" in text and "09:30" in text
                    and (root / "notes.txt").read_bytes() == b"Meeting: Tuesday at 09:30.\n")
        if case == "tools":
            return ((root / "answer.txt").read_text(encoding="utf-8").strip() == "42"
                    and (root / "input.txt").read_bytes() == b"21\n"
                    and {"read_file", "run_command"} <= set(tool_names))
    except (OSError, ValueError, UnicodeError):
        return False
    return False


def prepare(case, root):
    if case == "information":
        return ('Use only these facts: owner=Lin, city=Tokyo, items=3. '
                'Return exactly one JSON object with those three keys and values, no markdown. '
                'This is information organization; no tools or file changes are needed.')
    if case == "documents":
        (root / "notes.txt").write_bytes(b"Meeting: Tuesday at 09:30.\n")
        return ('Read notes.txt and create summary.txt containing the weekday and time. '
                'Keep notes.txt unchanged. Read back summary.txt to check your result.')
    (root / "input.txt").write_bytes(b"21\n")
    return (f'Read input.txt, then use run_command with argv[0]={json.dumps(sys.executable)} '
            'to multiply the number by 2 and write answer.txt. Keep input.txt unchanged. '
            'Read back answer.txt, then run a verification command that asserts it equals 42.')


async def trial(case, output_dir, budget, key, timeout, api_family="openai_chat_completions"):
    from lingnexus.app import Application
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    trial_id = "agent-" + case + "-" + uuid4().hex[:12]
    transport = BudgetTransport(httpx.AsyncHTTPTransport(retries=0), budget, trial_id, DEFAULT_MODEL,
                                capture_dir=output_dir / "provider-traces", secret=key)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ling-agent-eval-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        objective = prepare(case, workspace)
        app = Application(root / "data" / "ling.db", credential_cache=False)
        runtime = None
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            try:
                agent = app.create_agent("通用任务评测")
                profile = app.profiles.save("评测模型", ModelProfile(
                    api_family=APIFamily(api_family), model=DEFAULT_MODEL,
                    base_url=DEFAULT_BASE_URL, max_output_tokens=512, context_window_tokens=32768,
                    input_price_per_million=INPUT_RATE, output_price_per_million=OUTPUT_RATE,
                ), api_key=key)
                app.profiles.select(profile["id"], None)
                app.permissions.configure(agent, readable=[workspace], writable=[workspace], expected_revision=0)
                runtime = TaskRuntime(app, client)
                await runtime.start()
                tid = await runtime.submit({"agent_id": agent, "objective": objective,
                    "request_id": trial_id, **({"workspace": str(workspace)} if case != "information" else {})})
                deadline = time.monotonic() + timeout
                while runtime.get(tid)["status"] in {"queued", "running"} and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                task = runtime.get(tid)
                timed_out = task["status"] in {"queued", "running"}
                if timed_out:
                    await runtime.cancel(tid)
                    task = runtime.get(tid)
                events = runtime.events(tid, limit=1000)
                usage = {e["data"].get("step", 0): e["data"]["usage"] for e in events
                         if e["type"] == "usage" and e["data"].get("usage") is not None}
                budget.finalize_trial(trial_id, usage)
                names = [e["data"].get("name") for e in events if e["type"] == "tool_result"]
                artifact_root = runtime.workspace(tid) if case != "information" else workspace
                passed = (not timed_out and task["status"] in {"completed", "needs_review"}
                          and accept(case, artifact_root, task.get("final") or "", names))
                error = task.get("error_code")
                infrastructure = timed_out or bool(error and (
                    error.startswith("model_") or error in {"task_runtime_error", "credential_echo", "task_state_invalid"}))
                status = ("budget_limited" if transport.budget_limited else "framework_failure" if infrastructure
                          else "passed" if passed else "goal_not_completed")
                hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifact_root.iterdir()
                          if p.name in {"notes.txt", "summary.txt", "input.txt", "answer.txt"} and p.is_file()}
                record = {"case": case, "trial_id": trial_id, "status": status, "accepted": passed,
                    "api_family": api_family,
                    "task": {k: task.get(k) for k in ("status", "error_code", "step_count", "verified")},
                    "diagnostics": [e["data"] for e in events if e["type"] == "diagnostic"],
                    "tools": names, "artifact_hashes": hashes, "seconds": round(time.monotonic()-started, 3),
                    "budget": budget.snapshot(trial_id), "provider_traces": transport.trace_records}
                return record
            finally:
                if runtime is not None:
                    await runtime.aclose()
                await app.sandbox.sessions.aclose()
                app.profiles.clear_credentials()
                app.close()
                if "record" in locals():
                    record["cleanup"] = {"runtime_closed": True, "sessions_closed": True,
                                         "credentials_cleared": True, "temporary_data_removed_on_return": True}


def dry_run():
    results = []
    with tempfile.TemporaryDirectory(prefix="ling-agent-verifier-") as directory:
        root = Path(directory)
        for case in CASES:
            prepare(case, root)
            if case == "documents":
                (root / "summary.txt").write_text("Tuesday 09:30", encoding="utf-8")
            if case == "tools":
                (root / "answer.txt").write_text("42", encoding="utf-8")
            final = '{"owner":"Lin","city":"Tokyo","items":3}'
            positive = accept(case, root, final, ["read_file", "run_command"])
            if case == "documents":
                (root / "summary.txt").write_text("wrong", encoding="utf-8")
            if case == "tools":
                (root / "answer.txt").write_text("43", encoding="utf-8")
            negative = not accept(case, root, "{}", [])
            results.append({"case": case, "correct_accepted": positive, "wrong_rejected": negative})
    return {"mode": "offline_verifier", "network_calls": 0, "results": results}


async def live(args):
    key = os.environ.get("LINGNEXUS_EVAL_API_KEY", "")
    if not key:
        raise ValueError("credential_missing")
    budget = BudgetFile(DEFAULT_BUDGET, DEFAULT_BUDGET_USD)
    records = []
    for case in args.case or CASES:
        record = await trial(case, args.output_dir, budget, key, args.timeout_seconds, args.api_family)
        records.append(record)
        if record["status"] in {"framework_failure", "budget_limited"}:
            break
    return {"mode": "live_product", "source": _source_identity(Path(__file__).absolute().parents[1]),
        "model": DEFAULT_MODEL, "base_url": DEFAULT_BASE_URL, "records": records,
        "budget": budget.snapshot(), "remaining_categories": ["browser", "interruption_recovery"],
        "complete_general_acceptance": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--api-family", choices=("openai_chat_completions", "openai_responses"),
                        default="openai_chat_completions")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/agent-eval"))
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 1200:
        parser.error("timeout-seconds must be 1..1200")
    if args.live:
        result = asyncio.run(live(args))
        args.output_dir.mkdir(parents=True, exist_ok=True)
        path = args.output_dir / ("agent-eval-" + uuid4().hex + ".json")
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(path), "statuses": [r["status"] for r in result["records"]],
                          "budget": result["budget"]}))
    else:
        result = dry_run()
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
