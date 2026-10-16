"""Owner-run M1 acceptance: isolated fixtures, real model/tools, shared budget.

Dry-run is the default. Only --live sends paid requests. Browser approvals in
this controller are restricted to its own loopback form and fixture value.
Every attempt is retained; there are no automatic task retries.
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
from evaluate_agent import trial
from evaluate_coding import (
    DEFAULT_BASE_URL,
    DEFAULT_BUDGET,
    DEFAULT_BUDGET_USD,
    DEFAULT_MODEL,
    INPUT_RATE,
    OUTPUT_RATE,
    BudgetFile,
    BudgetTransport,
    _run_verifier_process,
    _source_identity,
    load_dataset,
)

CASES = ("information", "source_information", "documents", "document_delivery",
         "browser_pages", "browser_approval", "local_bug", "cross_file_feature",
         "tools", "interruption_recovery")
HIGH_RISK = ("document_delivery", "browser_approval", "cross_file_feature", "interruption_recovery")
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


def browser_goal(case, final, origin, *, read_confirmed, submissions, values):
    """Business evidence is separate from the runtime's conservative global flag."""
    if case == "browser_approval":
        return read_confirmed and submissions == 1 and values == [{"code": ["ACCEPT-42"]}]
    room = re.search(r"(?:\bRoom\s*7\b|7\s*号房间|房间\s*[：:]?\s*7\b)", final, re.IGNORECASE)
    source = re.search(re.escape(origin + "/details") + r"(?![A-Za-z0-9/_?%#&=.+-])", final)
    return (bool(room) and "09:30" in final and bool(source)
            and any(day in final for day in ("Tuesday", "周二", "星期二"))
            and any(place in final for place in ("Atrium", "中庭")) and submissions == 0)


@contextlib.contextmanager
def evaluation_directory():
    root = Path(tempfile.mkdtemp(prefix="ling-m1-live-")).resolve()
    cleanup = {"temporary_data_removed": False}
    try:
        yield root, cleanup
    finally:
        # Chrome may finish background profile writes just after context.close().
        # Verify the exact directory created here before each recursive cleanup.
        for _ in range(6):
            if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("ling-m1-live-"):
                raise ValueError("unexpected_evaluation_directory")
            if root.is_symlink():
                raise ValueError("linked_evaluation_directory")
            try:
                shutil.rmtree(root)
            except OSError:
                pass  # A disappearing nested Chrome file does not prove root removal.
            if not root.exists():
                cleanup["temporary_data_removed"] = True
                break
            time.sleep(0.2)


@contextlib.contextmanager
def fixture_site():
    state = {"submissions": 0, "values": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/details":
                self.reply('<h1>Room 7</h1><p>Workshop: Tuesday 09:30, Atrium.</p>')
            elif self.path == "/form":
                self.reply('<form method="post" action="/submit"><label>Code '
                           '<input name="code" id="code"></label><button id="submit">Send</button></form>')
            else:
                self.reply('<h1>Workshop directory</h1><a href="/details">Workshop details</a>')

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/submit" or not 0 <= length <= 1024:
                self.send_error(400)
                return
            state["values"].append(parse_qs(self.rfile.read(length).decode()))
            state["submissions"] += 1
            self.reply('<h1>Recorded ACCEPT-42</h1>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def prepare(case, workspace, origin):
    if case in {"local_bug", "cross_file_feature"}:
        spec = next(t for t in load_dataset()["tasks"] if t["id"] == case)
        for name, text in spec["files"].items():
            (workspace / name).write_text(text, encoding="utf-8")
        return spec["objective"]
    if case == "document_delivery":
        (workspace / "note1.txt").write_text("Tuesday 09:30\n", encoding="utf-8")
        (workspace / "note2.txt").write_text("Atrium Room 7\n", encoding="utf-8")
        return ("Read both note1.txt and note2.txt. Create a.txt with the day/time and b.txt with the "
                "venue/room. Preserve the notes. Read back both new files; do not write to the source directory.")
    if case == "interruption_recovery":
        (workspace / "slow.py").write_text(
            "import time\nfrom pathlib import Path\n"
            "p=Path('starts.txt')\np.write_text(p.read_text()+'start\\n' if p.exists() else 'start\\n')\n"
            "print('STARTED',flush=True)\ntime.sleep(8)\nPath('slow-result.txt').write_text('finished')\n",
            encoding="utf-8")
        return (f"Run slow.py exactly once using run_command argv[0]={json.dumps(sys.executable)}, "
                "purpose=work and long_running=true. Do not modify slow.py. The owner may interrupt the "
                "command. If interrupted, do not run it again: inspect starts.txt, create resumed.txt "
                "with 'safe continuation', and verify with a command that starts.txt has exactly one line "
                "and resumed.txt exists. Report whether the original command stopped or finished.")
    if case == "browser_approval":
        return (f"Use browser tools to inspect {origin}/form, fill #code with ACCEPT-42, then request "
                "a click on #submit. Wait for separate owner approval. After approval, read the page "
                "with expected_text='Recorded ACCEPT-42', then report the observed result. "
                "Submit exactly once. Do not use commands or HTTP libraries.")
    if case == "source_information":
        return (f"Read {origin}/details with browser tools. Return exactly one JSON object with keys "
                "weekday, time, venue, room (an integer) and source_url, using the observed page facts and its URL. "
                "No markdown, file changes or commands. Do not guess facts you have not read.")
    return (f"Use browser tools to inspect {origin}/ and its Workshop details link. Find the room, "
            "weekday, time and venue. Report those four facts and the exact details page URL as your source. "
            "Use only this granted fixture site. No commands or HTTP libraries.")


async def delivery_checks(runtime, app, tid, source):
    from lingnexus.common.errors import Conflict

    review = runtime.changes(tid)
    assert {f["path"] for f in review["files"]} == {"a.txt", "b.txt"}
    (source / "a.txt").write_text("owner edit", encoding="utf-8")
    try:
        await runtime.apply(tid, review_id=review["review_id"])
    except Conflict:
        pass
    else:
        raise AssertionError("conflicting source was not rejected")
    assert (source / "a.txt").read_text() == "owner edit"
    (source / "a.txt").unlink()  # Remove only this controller's conflict fixture.
    real_execute = app.actions.execute

    def fail_second(agent, request, name, args):
        if args["path"].endswith("b.txt"):
            raise OSError("controlled interruption before second effect")
        return real_execute(agent, request, name, args)

    app.actions.execute = fail_second
    try:
        stopped = await runtime.apply(tid, review_id=review["review_id"])
    finally:
        app.actions.execute = real_execute
    assert stopped["status"] == "unknown"
    remaining = await runtime.reconcile(tid, review_id=review["review_id"])
    assert [f["path"] for f in remaining["files"]] == ["b.txt"]
    result = await runtime.apply(tid, review_id=remaining["review_id"])
    assert result["status"] == "applied"
    assert await runtime.apply(tid, review_id=remaining["review_id"]) == result
    assert runtime.store.get_review(tid, review["review_id"])["status"] == "unknown"
    return {"source_conflict_refused": True, "remaining_only": True, "repeated_apply_no_replay": True}


async def custom_trial(case, output_dir, budget, key, timeout, api_family="openai_chat_completions"):
    from lingnexus.app import Application
    from lingnexus.browser.policy import OriginGrant
    from lingnexus.browser.service import BrowserService
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    trial_id = "m1-" + case + "-" + uuid4().hex[:12]
    transport = BudgetTransport(httpx.AsyncHTTPTransport(retries=0), budget, trial_id, DEFAULT_MODEL,
                                capture_dir=output_dir / "provider-traces", secret=key)
    started = time.monotonic()
    browser_case = case in {"browser_pages", "browser_approval", "source_information"}
    with evaluation_directory() as (root, cleanup), fixture_site() as (origin, site):
        workspace = root / "workspace"
        workspace.mkdir()
        objective = prepare(case, workspace, origin)
        originals = {p.name: p.read_bytes() for p in workspace.iterdir()}
        app = Application(root / "data" / "ling.db", credential_cache=False)
        runtime = None
        browser = None
        checks = {}
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            try:
                agent = app.create_agent("M1验收夹具")
                saved = app.profiles.save("验收模型", ModelProfile(
                    api_family=APIFamily(api_family), model=DEFAULT_MODEL,
                    base_url=DEFAULT_BASE_URL, max_output_tokens=1024, context_window_tokens=32768,
                    input_price_per_million=INPUT_RATE, output_price_per_million=OUTPUT_RATE), api_key=key)
                app.profiles.select(saved["id"], None)
                app.permissions.configure(agent, readable=[workspace], writable=[workspace], expected_revision=0)
                if browser_case:
                    browser = BrowserService(app.database, root / "data", executable_path=CHROME)
                    app.browser = browser
                    await browser.start()
                    await browser.configure_origins(agent, [OriginGrant(origin=origin, allow_local=True)], expected_revision=0)
                runtime = TaskRuntime(app, client)
                await runtime.start()
                tid = await runtime.submit({"agent_id": agent, "objective": objective, "request_id": trial_id,
                    **({"workspace": str(workspace)} if not browser_case else {})})
                interrupted = False
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    task = runtime.get(tid)
                    staged = runtime.workspace(tid) if not browser_case else workspace
                    if case == "interruption_recovery" and not interrupted and (staged / "starts.txt").exists():
                        await runtime.pause(tid)
                        sessions = app.sandbox.task_sessions(agent, tid)
                        assert sessions and all(s["status"] not in {"running", "cancelling", "unknown"} for s in sessions)
                        checks["interrupted_sessions_resolved"] = True
                        interrupted = True
                        await runtime.steer(tid, "The owner has interrupted the command. Inspect starts.txt, do not rerun slow.py. "
                                            "Now create resumed.txt and run the requested verification.")
                        continue
                    if browser_case and task["status"] == "waiting_owner":
                        assert case == "browser_approval" and site["submissions"] == 0
                        pending = browser.pending_approvals(agent)
                        assert len(pending) == 1
                        approval = pending[0]
                        intent = approval["intent"]
                        assert intent["selector"] == "#submit" and intent["form"]["action"] == origin + "/submit"
                        fields = intent["form"]["fields"]
                        assert any(f["name"] == "code" and f["value"] == "ACCEPT-42" for f in fields)
                        result = await browser.decide(approval["id"], decision="approve",
                            expected_intent_hash=approval["intent_hash"], agent_id=agent)
                        assert result["status"] == "completed"
                        await browser.decide(approval["id"], decision="approve",
                            expected_intent_hash=approval["intent_hash"], agent_id=agent)
                        assert site["submissions"] == 1
                        checks["no_submit_before_approval"] = True
                        checks["repeated_approval_no_replay"] = True
                        await runtime.resume(tid)
                        continue
                    if task["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.05)
                task = runtime.get(tid)
                timed_out = task["status"] in {"queued", "running"}
                if timed_out:
                    await runtime.cancel(tid)
                    task = runtime.get(tid)
                events = runtime.events(tid, limit=1000)
                budget.finalize_trial(trial_id, {e["data"].get("step", 0): e["data"]["usage"] for e in events
                                                if e["type"] == "usage" and e["data"].get("usage") is not None})
                names = [e["data"].get("name") for e in events if e["type"] == "tool_result"]
                final = task.get("final") or ""
                passed = not timed_out and task["status"] in {"completed", "needs_review"}
                if passed and case in {"local_bug", "cross_file_feature"}:
                    passed = await asyncio.to_thread(_run_verifier_process, staged, case, timeout=8)
                    passed = passed and task["verified"]
                elif passed and case == "document_delivery":
                    passed = ("Tuesday" in (staged / "a.txt").read_text() and "09:30" in (staged / "a.txt").read_text()
                              and "Atrium" in (staged / "b.txt").read_text() and "7" in (staged / "b.txt").read_text()
                              and all((staged / name).read_bytes() == data for name, data in originals.items()))
                    if passed:
                        checks.update(await delivery_checks(runtime, app, tid, workspace))
                elif passed and case == "interruption_recovery":
                    passed = (interrupted and (staged / "starts.txt").read_text().splitlines() == ["start"]
                              and (staged / "resumed.txt").read_text().strip() == "safe continuation"
                              and (staged / "slow.py").read_bytes() == originals["slow.py"] and task["verified"])
                elif passed and case == "browser_approval":
                    reads = [e["data"]["result"] for e in events
                             if e["type"] == "tool_result" and e["data"].get("name") == "browser_read"]
                    last_read = reads[-1] if reads else {}
                    checks["actual_confirmation_read"] = bool(
                        last_read.get("status") == "completed" and last_read.get("verification_passed")
                        and not (last_read.get("result") or {}).get("truncated")
                        and "Recorded ACCEPT-42" in (last_read.get("result") or {}).get("text", ""))
                    checks["submissions"] = site["submissions"]
                    checks["submitted_values_match"] = site["values"] == [{"code": ["ACCEPT-42"]}]
                    passed = (browser_goal(case, final, origin, read_confirmed=checks["actual_confirmation_read"],
                                submissions=site["submissions"], values=site["values"])
                              and bool(checks.get("no_submit_before_approval")))
                elif passed and case == "source_information":
                    try:
                        passed = json.loads(final.strip()) == {"weekday": "Tuesday", "time": "09:30",
                            "venue": "Atrium", "room": 7, "source_url": origin + "/details"}
                    except ValueError:
                        passed = False
                    passed = passed and "browser_read" in names and site["submissions"] == 0
                elif passed:
                    passed = (browser_goal(case, final, origin, read_confirmed=False,
                                submissions=site["submissions"], values=site["values"])
                              and {"browser_goto", "browser_read"} <= set(names))
                error = task.get("error_code")
                infra = timed_out or bool(error and (error.startswith("model_") or error == "task_runtime_error"))
                record = {"case": case, "trial_id": trial_id, "accepted": bool(passed),
                    "status": "budget_limited" if transport.budget_limited else "framework_failure" if infra
                              else "passed" if passed else "goal_not_completed",
                    "api_family": api_family,
                    "task": {k: task.get(k) for k in ("status", "error_code", "step_count", "verified")},
                    "checks": checks, "tools": names, "diagnostics": [e["data"] for e in events if e["type"] == "diagnostic"],
                    "seconds": round(time.monotonic() - started, 3), "budget": budget.snapshot(trial_id),
                    "provider_traces": transport.trace_records, "cleanup": cleanup}
                return record
            finally:
                if runtime:
                    await runtime.aclose()
                await app.sandbox.sessions.aclose()
                if browser:
                    await browser.aclose()
                app.profiles.clear_credentials()
                app.close()


async def run(args):
    key = os.environ.get("LINGNEXUS_EVAL_API_KEY", "")
    if not key:
        raise ValueError("credential_missing")
    budget = BudgetFile(DEFAULT_BUDGET, DEFAULT_BUDGET_USD)
    schedule = [(case, "normal") for case in CASES] + [(case, "repeat") for _ in range(2) for case in HIGH_RISK]
    if args.case:
        schedule = [(case, "selected") for case in args.case]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / ("m1-acceptance-" + uuid4().hex + ".json")
    report = {"source": _source_identity(Path(__file__).absolute().parents[1]), "model": DEFAULT_MODEL,
              "base_url": DEFAULT_BASE_URL, "api_family": args.api_family,
              "schedule": schedule, "records": [], "complete": False}
    previous_failure = None
    for case, group in schedule:
        try:
            if case in {"information", "documents", "tools"}:
                record = await trial(case, args.output_dir, budget, key, args.timeout_seconds, args.api_family)
            else:
                record = await custom_trial(case, args.output_dir, budget, key, args.timeout_seconds, args.api_family)
            record["group"] = group
            report["records"].append(record)
        except Exception as error:
            report["controller_error"] = {"case": case, "type": type(error).__name__}
            report["budget"] = budget.snapshot()
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        report["budget"] = budget.snapshot()
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": case, "group": group, "status": record["status"], "report": str(path)}), flush=True)
        failure = (record.get("task", {}).get("error_code") or "model_timeout") if record["status"] == "framework_failure" else None
        if record["status"] == "budget_limited" or (failure and failure == previous_failure):
            report["stop_reason"] = "budget_or_repeated_infrastructure_failure"
            break
        previous_failure = failure
    report["complete"] = len(report["records"]) == len(schedule)
    normal = [r for r in report["records"] if r["group"] == "normal"]
    report["accepted"] = (not args.case and report["complete"] and sum(r["accepted"] for r in normal) >= 9
                          and all(r["accepted"] for r in report["records"] if r["case"] in HIGH_RISK))
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "complete": report["complete"], "accepted": report["accepted"],
                      "budget": budget.snapshot()}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--api-family", choices=("openai_chat_completions", "openai_responses"),
                        default="openai_chat_completions")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/m1-acceptance"))
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 1200:
        parser.error("timeout-seconds must be 1..1200")
    if args.live:
        asyncio.run(run(args))
    else:
        print(json.dumps({"network_calls": 0, "normal": CASES, "repeat_twice": HIGH_RISK, "planned": 18}))


if __name__ == "__main__":
    main()
