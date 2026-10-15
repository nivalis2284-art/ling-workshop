from __future__ import annotations

import asyncio
import multiprocessing
from decimal import Decimal
from types import SimpleNamespace

import httpx

from scripts.evaluate_coding import (
    _VERIFIER,
    DEFAULT_DATASET,
    BudgetExceeded,
    BudgetFile,
    BudgetTransport,
    _probe_delivery_conflict,
    _provider_transport,
    _run_acceptance,
    _run_verifier_process,
    _source_identity,
    dry_run,
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

