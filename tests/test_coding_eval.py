from __future__ import annotations

import asyncio
import json
import multiprocessing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

from scripts.evaluate_coding import (
    _VERIFIER,
    DEFAULT_DATASET,
    BudgetExceeded,
    BudgetFile,
    BudgetTransport,
    _command_durations,
    _failure_origin,
    _probe_delivery_conflict,
    _provider_transport,
    _run_acceptance,
    _run_verifier_process,
    _safe_changes,
    _safe_exception_diagnostic,
    _sandbox_resource_report,
    _slow_command_passed,
    _source_identity,
    _wait_for_task,
    dry_run,
    load_dataset,
)


def _reserve_from_process(path: str, queue) -> None:
    budget = BudgetFile(path, Decimal("0.08"))
    try:
        budget.reserve("concurrent", "test-model", b"x" * 500_000, 1)
    except BudgetExceeded:
        queue.put(False)
    else:
        queue.put(True)


def test_fixed_fixture_versions_have_expected_acceptance() -> None:
    result = dry_run(DEFAULT_DATASET)
    assert result["status"] == "passed"
    assert result["network_calls"] == 0
    assert len(result["cases"]) == 6
    assert all(item["correct_accepted"] and item["incorrect_rejected"]
               for item in result["cases"])


def test_budget_transport_rejects_before_forwarding(tmp_path) -> None:
    calls = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    budget = BudgetFile(tmp_path / "budget.json", "0.0001")
    transport = BudgetTransport(httpx.MockTransport(upstream), budget, "trial", "test-model")

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post("https://provider.invalid/v1/chat/completions",
                                     json={"max_tokens": 1024, "messages": [{"content": "x"}]})

    response = asyncio.run(send())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "budget_limited"
    assert transport.budget_limited is True
    assert calls == []
    assert budget.snapshot("trial")["budget_limited_count"] == 1


def test_budget_file_cap_is_shared_across_processes(tmp_path) -> None:
    path = str(tmp_path / "shared-budget.json")
    BudgetFile(path, "0.08")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_reserve_from_process, args=(path, queue)) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    outcomes = [queue.get(timeout=3) for _ in workers]
    snapshot = BudgetFile(path, "0.08").snapshot()
    assert sum(outcomes) == 1
    assert Decimal(snapshot["reserved_usd"]) <= Decimal("0.08")
    assert snapshot["budget_limited_count"] == 5


def test_explicit_optional_proxy_keeps_environment_proxy_disabled(monkeypatch) -> None:
    seen = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("scripts.evaluate_coding.httpx.AsyncHTTPTransport", FakeTransport)
    monkeypatch.setenv("LINGNEXUS_EVAL_PROXY", "http://127.0.0.1:8765")
    _provider_transport()
    assert seen == {"proxy": "http://127.0.0.1:8765", "trust_env": False}
    seen.clear()
    monkeypatch.delenv("LINGNEXUS_EVAL_PROXY")
    _provider_transport()
    assert seen == {"proxy": None, "trust_env": False}


def test_hidden_verifier_rejects_code_that_passes_visible_examples(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "calculator.py").write_text(
        "def add(left, right):\n"
        "    return left + right if left < 100 else 0\n",
        encoding="utf-8",
    )
    assert "12345678901234567890" in _VERIFIER
    assert _run_verifier_process(project, "local_bug", timeout=5) is False


def test_behavior_refactor_requires_real_helper_extraction(tmp_path) -> None:
    project = tmp_path / "pricing"
    project.mkdir()
    initial = load_dataset()["tasks"]
    task = next(item for item in initial if item["id"] == "behavior_refactor")
    source = task["files"]["pricing.py"]
    (project / "pricing.py").write_text(source, encoding="utf-8")
    # Correct old behavior alone is not evidence that a refactor occurred.
    assert _run_verifier_process(project, "behavior_refactor", timeout=5) is False
    (project / "pricing.py").write_text(
        "def _format_price_value(cents, currency):\n"
        "    amount = f\"{abs(cents) / 100:.2f}\"\n"
        "    prefix = \"$\" if currency == \"USD\" else currency + \" \"\n"
        "    value = prefix + amount\n"
        "    if cents < 0:\n        value = \"(\" + value + \")\"\n"
        "    return value\n\n"
        "def format_price(cents, currency=\"USD\"):\n"
        "    return _format_price_value(cents, currency)\n",
        encoding="utf-8",
    )
    assert _run_verifier_process(project, "behavior_refactor", timeout=5) is True


def test_slow_command_uses_persisted_session_duration_and_success(tmp_path) -> None:
    session_id = "session-1"
    started = "2026-10-15T00:00:00+00:00"
    finished = "2026-10-15T00:00:31.250000+00:00"
    events = [
        {"id": 1, "type": "tool_started", "created_at": started,
         "data": {"step_id": "step-1", "name": "run_command"}},
        # The parent tool returns quickly; this timestamp must not define duration.
        {"id": 2, "type": "tool_result", "created_at": "2026-10-15T00:00:01+00:00",
         "data": {"step_id": "step-1", "name": "run_command", "result": {
             "status": "running", "execution": {"session_id": session_id},
         }}},
    ]
    sessions = [{
        "session_id": session_id,
        "updated_at": finished,
        "record": {
            "session_id": session_id,
            "created_at": started,
            "status": "success",
            "argv": ["python", "slow_check.py"],
            "result": {"status": "success", "exit_code": 0, "timed_out": False},
        },
    }]
    commands = _command_durations(events, sessions)
    assert commands[0]["duration_seconds"] == 31.25
    assert _slow_command_passed(commands) is True
    assert _slow_command_passed([{**commands[0], "status": "timeout", "exit_code": None}]) is False
    assert _slow_command_passed([{**commands[0], "argv": ["python", "other.py"]}]) is False
    assert _command_durations(events, [])[0:] == []


def test_wait_for_task_sends_guidance_once_after_a_tool_result() -> None:
    class Runtime:
        def __init__(self):
            self.cursor_requests = []
            self.guidance = []
            self.poll = 0

        def events(self, task_id, *, after, limit):
            self.cursor_requests.append(after)
            self.poll += 1
            if self.poll == 1:
                return [{"id": 3, "type": "tool_result", "data": {}}]
            return []

        async def steer(self, task_id, text):
            self.guidance.append((task_id, text))

        def get(self, task_id):
            return {"status": "running" if self.poll == 1 else "completed"}

    runtime = Runtime()
    result = asyncio.run(_wait_for_task(
        runtime, "task-1", {"owner_guidance": "preserve local-part case"}, timeout=1,
    ))
    assert result == {"status": "completed", "guidance_sent": True}
    assert runtime.guidance == [("task-1", "preserve local-part case")]
    assert runtime.cursor_requests == [0, 3]


def test_safe_exception_diagnostic_has_locations_without_message_or_paths() -> None:
    try:
        raise RuntimeError("secret-token-should-never-appear")
    except RuntimeError as exc:
        diagnostic = _safe_exception_diagnostic(exc, origin="product_runtime")
    encoded = json.dumps(diagnostic)
    assert diagnostic["origin"] == "product_runtime"
    assert diagnostic["exception_type"] == "RuntimeError"
    assert diagnostic["frames"]
    assert set(diagnostic["frames"][-1]) == {"file", "line", "function"}
    assert diagnostic["frames"][-1]["file"] == "test_coding_eval.py"
    assert "secret-token" not in encoded
    assert "C:\\Users" not in encoded


def test_failure_origin_separates_product_lifecycle_from_controller() -> None:
    assert _failure_origin("product_setup") == "product_runtime"
    assert _failure_origin("product_execution") == "product_runtime"
    assert _failure_origin("evaluation_collection") == "evaluation_controller"


def test_sandbox_resource_report_keeps_runtime_limit_fallback_truthful() -> None:
    from lingnexus.tools import ExecutionLimits

    runtime_limits = ExecutionLimits(timeout_seconds=120, max_output_bytes=262144)

    class LegacySandbox:
        def info(self):
            return {"level": "local", "preset": "conservative", "limits": {"timeout_seconds": 30}}

    report = _sandbox_resource_report(LegacySandbox(), runtime_limits, preset="legacy_runtime_limits")
    assert report["preset"] == "legacy_runtime_limits"
    assert report["resource_source"] == "runtime_limits"
    assert report["limits"]["timeout_seconds"] == 120
    assert report["limits"]["max_output_bytes"] == 262144
    assert report["limits"]["memory_bytes"] == 268435456


def test_sandbox_resource_report_records_coding_preset_limits() -> None:
    class CodingSandbox:
        def info(self):
            return {"level": "container", "preset": "coding", "limits": {
                "timeout_seconds": 120, "max_output_bytes": 262144,
                "memory_bytes": 1_073_741_824, "session_timeout_seconds": 300,
                "max_sessions": 4,
            }}

    report = _sandbox_resource_report(CodingSandbox(), object(), preset="coding")
    assert report["preset"] == "coding"
    assert report["resource_source"] == "sandbox_preset"
    assert report["limits"]["memory_bytes"] == 1_073_741_824
    assert report["session_timeout_seconds"] == 300
    assert report["max_sessions"] == 4


def test_blocked_task_changes_conflict_is_classified_as_empty_snapshot() -> None:
    from lingnexus.common.errors import Conflict

    class Runtime:
        def changes(self, task_id):
            raise Conflict("blocked task has no reviewable snapshot")

    assert _safe_changes(Runtime(), "blocked-1") == {"files": [], "review_id": None}


def test_source_identity_detects_export_and_hashes_sql_and_lockfiles(tmp_path, monkeypatch) -> None:
    source = tmp_path / "export"
    package = source / "src" / "lingnexus"
    package.mkdir(parents=True)
    (package / "app.py").write_text("app = 1\n", encoding="utf-8")
    (source / "src" / "lingnexus" / "migrations").mkdir()
    sql = source / "src" / "lingnexus" / "migrations" / "001.sql"
    sql.write_text("select 1;\n", encoding="utf-8")
    lock = source / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")

    def fake_git(command, **_kwargs):
        assert "--show-toplevel" in command
        return SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")

    monkeypatch.setattr("scripts.evaluate_coding.subprocess.run", fake_git)
    first = _source_identity(source)
    assert first["revision"] == "export/unknown"
    sql.write_text("select 2;\n", encoding="utf-8")
    second = _source_identity(source)
    assert second["source_hash"] != first["source_hash"]
    lock.write_text("version = 2\n", encoding="utf-8")
    third = _source_identity(source)
    assert third["source_hash"] != second["source_hash"]


def test_source_identity_prunes_unreadable_runtime_data_subtrees(tmp_path, monkeypatch) -> None:
    source = tmp_path / "export"
    (source / "src" / "lingnexus").mkdir(parents=True)
    (source / "src" / "lingnexus" / "app.py").write_text("app = 1\n", encoding="utf-8")
    ignored = source / ".ling-data" / "private"
    ignored.mkdir(parents=True)
    (ignored / "runtime.py").write_text("private = True\n", encoding="utf-8")

    def fake_git(command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if ".ling-data" in path.parts:
            raise PermissionError("should have been pruned")
        return original_read_bytes(path)

    monkeypatch.setattr("scripts.evaluate_coding.subprocess.run", fake_git)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    identity = _source_identity(source)
    assert identity["source_hash"]


def test_slow_delivery_conflict_probe_preserves_controller_change(tmp_path) -> None:
    target = tmp_path / "release.py"
    target.write_bytes(b"def build_report(): return {}\n")
    calls = []

    class Runtime:
        async def apply(self, task_id, *, review_id):
            calls.append((task_id, review_id))
            raise Conflict("stale original workspace")

    from lingnexus.common.errors import Conflict

    result = asyncio.run(_probe_delivery_conflict(Runtime(), tmp_path, "task-1", "review-1"))
    assert result["status"] == "passed"
    assert result["rejected_stale_review"] is True
    assert result["controller_change_preserved"] is True
    assert result["control_mutation_excluded_from_model_workspace_check"] is True
    assert calls == [("task-1", "review-1")]


def test_local_acceptance_reports_best_effort_boundary(tmp_path) -> None:
    seen = {}

    class Executor:
        def execute(self, command):
            seen["command"] = command
            return SimpleNamespace(status="success", exit_code=0, timed_out=False, error=None)

    class Sandbox:
        image = None

        def level(self):
            return "local"

        def executor(self, workspace, **kwargs):
            seen["workspace"] = workspace
            seen["kwargs"] = kwargs
            return Executor()

    app = SimpleNamespace(sandbox=Sandbox())
    result = _run_acceptance(app, tmp_path, "local_bug")
    assert result["passed"] is True
    assert result["isolation_level"] == "local"
    assert result["verifier_boundary"] == "local_best_effort_not_confidential"
    assert seen["kwargs"]["writable"] is False
    assert seen["kwargs"]["network_mode"] == "none"

