"""Evaluate six fixed coding tasks through the product TaskRuntime.

The controller owns the immutable acceptance checks and the shared spend cap.
An isolated child imports the requested product source tree for each trial.
Credentials are read only from LINGNEXUS_EVAL_API_KEY and never serialized.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import httpx

SCRIPT = Path(__file__).resolve()
REPOSITORY = SCRIPT.parents[1]
DEFAULT_DATASET = REPOSITORY / "tests" / "fixtures" / "coding_tasks" / "编程任务集.json"
DEFAULT_OUTPUT = REPOSITORY / ".artifacts" / "coding-eval"
DEFAULT_BUDGET = DEFAULT_OUTPUT / "budget.json"
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_BASE_URL = "https://cf.hetune.top"
DEFAULT_BUDGET_USD = Decimal("0.20")
INPUT_RATE = Decimal("0.13")
CACHED_INPUT_RATE = Decimal("0.0299")
OUTPUT_RATE = Decimal("0.4355")
PROTOCOL_MARGIN_TOKENS = 1024
DATASET_VERSION = "coding-tasks-v1"
TERMINAL = {"completed", "needs_review", "blocked", "paused", "cancelled", "interrupted", "waiting_owner"}


class BudgetExceeded(Exception):
    """A request was rejected before its bytes were sent to the provider."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    request_index: int
    input_bytes: int
    input_tokens_upper: int
    max_output_tokens: int
    reserve_usd: Decimal


class BudgetFile:
    """Cross-process locked JSON accounting with no credentials or request body."""

    _local_guard = threading.Lock()
    _local_locks: ClassVar[dict[str, threading.RLock]] = {}

    def __init__(self, path: str | Path, cap_usd: Decimal | str | float):
        self.path = Path(path)
        self.cap = Decimal(str(cap_usd))
        if not self.cap.is_finite() or self.cap <= 0:
            raise ValueError("budget_must_be_positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve())
        with self._local_guard:
            self._thread_lock = self._local_locks.setdefault(key, threading.RLock())
        with self._locked_state() as (state, stream):
            if state is None:
                self._write_state(stream, {
                    "schema_version": 1,
                    "budget_usd": _money(self.cap),
                    "reserved_usd": "0",
                    "actual_estimated_usd": "0",
                    "requests": [],
                })
            elif Decimal(state["budget_usd"]) != self.cap:
                raise ValueError("budget_file_cap_mismatch")

    def reserve(self, trial_id: str, model: str, body: bytes,
                max_output_tokens: int) -> Reservation:
        if not isinstance(body, bytes):
            raise TypeError("body must be UTF-8 encoded bytes")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("request_output_cap_missing")
        input_tokens_upper = len(body) + PROTOCOL_MARGIN_TOKENS
        amount = _upper_cost(input_tokens_upper, max_output_tokens)
        reservation_id = uuid4().hex
        with self._locked_state() as (state, stream):
            assert state is not None
            indexes = [
                item["request_index"] for item in state["requests"]
                if item.get("trial_id") == trial_id
            ]
            request_index = max(indexes, default=0) + 1
            reserved = Decimal(state["reserved_usd"])
            item = {
                "reservation_id": reservation_id,
                "trial_id": trial_id,
                "request_index": request_index,
                "model": model,
                "input_bytes": len(body),
                "protocol_margin_tokens": PROTOCOL_MARGIN_TOKENS,
                "input_tokens_upper": input_tokens_upper,
                "max_output_tokens": max_output_tokens,
                "requested_reserve_usd": _money(amount),
                "reserved_usd": "0",
                "actual_usage": None,
                "actual_estimated_usd": None,
                "status": "budget_limited",
            }
            if reserved + amount > self.cap:
                state["requests"].append(item)
                self._write_state(stream, state)
                raise BudgetExceeded("budget_limited")
            item["status"] = "reserved"
            item["reserved_usd"] = _money(amount)
            state["requests"].append(item)
            state["reserved_usd"] = _money(reserved + amount)
            self._write_state(stream, state)
        return Reservation(reservation_id, request_index, len(body), input_tokens_upper,
                           max_output_tokens, amount)

    def record_usage(self, trial_id: str, request_index: int, usage: dict | None) -> None:
        with self._locked_state() as (state, stream):
            assert state is not None
            item = next((record for record in state["requests"]
                         if record.get("trial_id") == trial_id
                         and record.get("request_index") == request_index), None)
            if item is None or item["status"] == "budget_limited":
                return
            safe = _safe_usage(usage)
            item["actual_usage"] = safe
            item["actual_estimated_usd"] = _money(_actual_cost(safe)) if safe else None
            item["status"] = "usage_recorded" if safe else "usage_unavailable"
            self._recalculate_actual(state)
            self._write_state(stream, state)

    def finalize_trial(self, trial_id: str, usage_by_step: dict[int, dict | None]) -> None:
        with self._locked_state() as (state, stream):
            assert state is not None
            for item in state["requests"]:
                if item.get("trial_id") != trial_id or item["status"] == "budget_limited":
                    continue
                usage = usage_by_step.get(item["request_index"])
                safe = _safe_usage(usage)
                item["actual_usage"] = safe
                item["actual_estimated_usd"] = _money(_actual_cost(safe)) if safe else None
                item["status"] = "usage_recorded" if safe else "usage_unavailable"
            self._recalculate_actual(state)
            self._write_state(stream, state)

    def snapshot(self, trial_id: str | None = None) -> dict:
        with self._locked_state() as (state, _stream):
            assert state is not None
            requests = [item for item in state["requests"]
                        if trial_id is None or item.get("trial_id") == trial_id]
            return {
                "budget_usd": state["budget_usd"],
                "reserved_usd": _money(sum((Decimal(item["reserved_usd"]) for item in requests), Decimal(0))),
                "actual_estimated_usd": _money(sum((Decimal(item["actual_estimated_usd"] or "0")
                                                      for item in requests), Decimal(0))),
                "request_count": len(requests),
                "budget_limited_count": sum(item["status"] == "budget_limited" for item in requests),
                "usage_unavailable_count": sum(item["status"] == "usage_unavailable" for item in requests),
                "remaining_reserved_capacity_usd": _money(
                    max(Decimal(0), self.cap - Decimal(state["reserved_usd"]))
                ),
            }

    @contextlib.contextmanager
    def _locked_state(self):
        with self._thread_lock:
            self.path.touch(exist_ok=True)
            mutex = _acquire_budget_mutex(self.path) if os.name == "nt" else None
            locked = False
            stream = None
            try:
                stream = self.path.open("r+b")
                if os.name != "nt":
                    _lock_file(stream)
                    locked = True
                stream.seek(0)
                content = stream.read()
                if not content.strip():
                    state = None
                else:
                    state = json.loads(content.decode("utf-8"))
                    _validate_budget_state(state)
                yield state, stream
            finally:
                if stream is not None:
                    if locked:
                        _unlock_file(stream)
                    stream.close()
                if mutex is not None:
                    _release_budget_mutex(mutex)

    def _write_state(self, stream, state: dict) -> None:
        stream.seek(0)
        stream.write(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())

    @staticmethod
    def _recalculate_actual(state: dict) -> None:
        state["actual_estimated_usd"] = _money(sum(
            (Decimal(item["actual_estimated_usd"] or "0") for item in state["requests"]),
            Decimal(0),
        ))


class BudgetTransport(httpx.AsyncBaseTransport):
    """Reserve worst-case spend before forwarding each complete HTTP request."""

    def __init__(self, inner: httpx.AsyncBaseTransport, budget: BudgetFile,
                 trial_id: str, model: str):
        self.inner = inner
        self.budget = budget
        self.trial_id = trial_id
        self.model = model
        self.budget_limited = False
        self.reservations: list[Reservation] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() != "POST":
            return await self.inner.handle_async_request(request)
        try:
            content = await request.aread()
            payload = json.loads(content)
            max_output_tokens = _max_output_tokens(payload)
            reservation = self.budget.reserve(
                self.trial_id, self.model, content, max_output_tokens
            )
        except BudgetExceeded:
            self.budget_limited = True
            return httpx.Response(
                429,
                json={"error": {"code": "budget_limited"}},
                request=request,
            )
        except (ValueError, TypeError, UnicodeError):
            # An unpriceable body is not allowed to leave the process.
            self.budget_limited = True
            return httpx.Response(
                429,
                json={"error": {"code": "budget_limited"}},
                request=request,
            )
        self.reservations.append(reservation)
        forwarded = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=content,
            extensions=request.extensions,
        )
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self.inner.aclose()


_VERIFIER = r'''import json, sys
sys.path.insert(0, ".")
case = sys.argv[1]
if case == "local_bug":
    from calculator import add
    assert add(17, 25) == 42 and add(-4, 9) == 5
    assert add(12345678901234567890, -12345678901234567800) == 90
elif case == "cross_file_feature":
    from summary import summarize
    got = summarize([{"name": " pen ", "quantity": "2"}, {"name": "book", "quantity": 7}])
    assert got == {"count": 2, "total_quantity": 9, "low_stock": ["pen"]}
    assert summarize([{"name": " bottle ", "quantity": 0}, {"name": "mug", "quantity": 3}]) == {
        "count": 2, "total_quantity": 3, "low_stock": ["bottle"]
    }
elif case == "behavior_refactor":
    from pricing import format_price
    assert format_price(1250) == "$12.50"
    assert format_price(-5) == "($0.05)"
    assert format_price(500, "JPY") == "JPY 5.00"
    assert format_price(0) == "$0.00"
elif case == "test_repair":
    from parser import parse_row
    assert parse_row('alpha,"beta,gamma",delta') == ["alpha", "beta,gamma", "delta"]
    assert parse_row('one,"say ""hi""",three') == ["one", 'say "hi"', "three"]
    assert parse_row("alpha,,delta,") == ["alpha", "", "delta", ""]
elif case == "guided_recovery":
    from normalizer import normalize_address
    assert normalize_address("  MiXeD@BÜCHER.example ") == "MiXeD@xn--bcher-kva.example"
    assert normalize_address("用户@bücher.example") == "用户@xn--bcher-kva.example"
    assert normalize_address("  Local+tag@BÜCHER.example  ") == "Local+tag@xn--bcher-kva.example"
    try:
        normalize_address("missing-at")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid address accepted")
    try:
        normalize_address("@example.com")
    except ValueError:
        pass
    else:
        raise AssertionError("empty local-part accepted")
elif case == "slow_delivery":
    from pathlib import Path
    from release import build_report
    expected = {"version": "1.2", "files": ["alpha.py", "zeta.py"], "artifact_count": 2}
    assert build_report("1.2", ["zeta.py", "alpha.py"]) == expected
    assert json.loads(Path("release.json").read_text(encoding="utf-8")) == expected
    assert build_report("0.0", []) == {"version": "0.0", "files": [], "artifact_count": 0}
else:
    raise AssertionError("unknown fixed case")
print("verified")
'''


_SOLUTIONS = {
    "local_bug": {"calculator.py": "def add(left, right):\n    return left + right\n"},
    "cross_file_feature": {
        "summary.py": (
            "from records import normalize_item\n\n"
            "def summarize(items):\n"
            "    normalized = [normalize_item(item) for item in items]\n"
            "    return {\n"
            "        \"count\": len(normalized),\n"
            "        \"total_quantity\": sum(item[\"quantity\"] for item in normalized),\n"
            "        \"low_stock\": [item[\"name\"] for item in normalized if item[\"quantity\"] <= 2],\n"
            "    }\n"
        ),
    },
    "test_repair": {
        "parser.py": (
            "import csv\nfrom io import StringIO\n\n"
            "def parse_row(line):\n"
            "    if not line:\n        return []\n"
            "    return next(csv.reader(StringIO(line), skipinitialspace=True))\n"
        ),
    },
    "guided_recovery": {
        "email_parts.py": (
            "def split_address(value):\n"
            "    if not isinstance(value, str):\n        raise ValueError(\"invalid email\")\n"
            "    parts = value.strip().split(\"@\")\n"
            "    if len(parts) != 2 or not all(parts):\n        raise ValueError(\"invalid email\")\n"
            "    local, domain = parts\n"
            "    if any(char.isspace() for char in local + domain):\n        raise ValueError(\"invalid email\")\n"
            "    return local, domain\n"
        ),
        "normalizer.py": (
            "from email_parts import split_address\n\n"
            "def normalize_address(value):\n"
            "    local, domain = split_address(value)\n"
            "    try:\n        domain = domain.encode(\"idna\").decode(\"ascii\").lower()\n"
            "    except UnicodeError:\n        raise ValueError(\"invalid email\") from None\n"
            "    return local + \"@\" + domain\n"
        ),
    },
    "slow_delivery": {
        "release.py": (
            "def build_report(version, files):\n"
            "    names = sorted(files)\n"
            "    return {\"version\": version, \"files\": names, \"artifact_count\": len(names)}\n"
        ),
        "release.json": '{"artifact_count":2,"files":["alpha.py","zeta.py"],"version":"1.2"}\n',
    },
}

_NEGATIVE_VERSIONS = {
    "behavior_refactor": {"pricing.py": "def format_price(cents, currency=\"USD\"):\n    return str(cents)\n"},
}


def load_dataset(path: str | Path = DEFAULT_DATASET) -> dict:
    dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    if dataset.get("version") != DATASET_VERSION:
        raise ValueError("dataset_version_invalid")
    tasks = dataset.get("tasks")
    expected_ids = {"local_bug", "cross_file_feature", "behavior_refactor", "test_repair",
                    "guided_recovery", "slow_delivery"}
    if not isinstance(tasks, list) or {item.get("id") for item in tasks} != expected_ids:
        raise ValueError("dataset_cases_invalid")
    for task in tasks:
        if (not isinstance(task.get("files"), dict) or not task["files"]
                or any(not _safe_relative(name) or not isinstance(content, str)
                       for name, content in task["files"].items())):
            raise ValueError("dataset_files_invalid")
    return dataset


def dry_run(dataset_path: str | Path = DEFAULT_DATASET) -> dict:
    dataset = load_dataset(dataset_path)
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="ling-coding-fixtures-") as temp:
        root = Path(temp)
        for task in dataset["tasks"]:
            case_dir = root / task["id"]
            _write_files(case_dir, task["files"])
            negative = _NEGATIVE_VERSIONS.get(task["id"])
            if negative:
                _write_files(case_dir, negative)
            incorrect_passed = _run_verifier_process(case_dir, task["id"], timeout=8)
            _write_files(case_dir, task["files"])
            solution = _SOLUTIONS.get(task["id"], {})
            _write_files(case_dir, solution)
            correct_passed = _run_verifier_process(case_dir, task["id"], timeout=8)
            outcomes.append({"case_id": task["id"], "incorrect_rejected": not incorrect_passed,
                             "correct_accepted": correct_passed})
    return {
        "status": "passed" if all(item["incorrect_rejected"] and item["correct_accepted"]
                                   for item in outcomes) else "failed",
        "dataset_version": DATASET_VERSION,
        "cases": outcomes,
        "network_calls": 0,
    }


def _run_verifier_process(workspace: Path, case_id: str, timeout: float) -> bool:
    try:
        for cache in workspace.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        result = subprocess.run(
            [sys.executable, "-I", "-c", _VERIFIER, case_id],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_clean_verifier_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"verified"


def _clean_verifier_env() -> dict[str, str]:
    # No model key or parent-only environment values enter verifier processes.
    allowed = ("PATH", "PATHEXT", "SystemRoot", "windir", "COMSPEC")
    env = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _write_files(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def _safe_relative(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _max_output_tokens(payload: dict) -> int:
    for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = payload.get(name)
        if type(value) is int and value > 0:
            return value
    raise ValueError("request_output_cap_missing")


def _upper_cost(input_tokens: int, output_tokens: int) -> Decimal:
    return (Decimal(input_tokens) * INPUT_RATE + Decimal(output_tokens) * OUTPUT_RATE) / Decimal(1_000_000)


def _actual_cost(usage: dict | None) -> Decimal:
    if not usage or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return Decimal(0)
    input_tokens = usage["input_tokens"]
    cached = min(input_tokens, usage.get("cached_input_tokens") or 0)
    return (
        Decimal(input_tokens - cached) * INPUT_RATE
        + Decimal(cached) * CACHED_INPUT_RATE
        + Decimal(usage["output_tokens"]) * OUTPUT_RATE
    ) / Decimal(1_000_000)


def _safe_usage(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    names = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
             "cached_input_tokens", "cache_creation_input_tokens")
    clean = {name: usage.get(name) for name in names}
    if any(value is not None and (type(value) is not int or value < 0)
           for value in clean.values()):
        return None
    return clean if clean["input_tokens"] is not None or clean["output_tokens"] is not None else None


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")


def _validate_budget_state(state: Any) -> None:
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or not isinstance(state.get("requests"), list)
            or not isinstance(state.get("budget_usd"), str)
            or not isinstance(state.get("reserved_usd"), str)):
        raise ValueError("budget_file_corrupt")
    for item in state["requests"]:
        if not isinstance(item, dict) or not all(
            key in item for key in ("reservation_id", "trial_id", "request_index", "reserved_usd", "status")
        ):
            raise ValueError("budget_file_corrupt")


def _lock_file(stream) -> None:
    stream.seek(0)
    import fcntl
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_file(stream) -> None:
    import fcntl
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _acquire_budget_mutex(path: Path):
    import ctypes
    digest = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, f"Local\\LingCodingBudget-{digest}")
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    wait = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
    if wait not in (0, 0x80):
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
    return (kernel32, ctypes.c_void_p(handle))


def _release_budget_mutex(mutex) -> None:
    kernel32, handle = mutex
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


async def _trial_child(args) -> dict:
    product_source = _resolve_product_source(args.product_source)
    source_dir = product_source / "src"
    sys.path.insert(0, str(source_dir))
    from lingnexus.app import Application
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime
    from lingnexus.tools import ExecutionLimits

    dataset = load_dataset(args.dataset)
    task = next(item for item in dataset["tasks"] if item["id"] == args.case_id)
    key = os.environ.get("LINGNEXUS_EVAL_API_KEY", "")
    if not key:
        return {"status": "framework_failure", "error_code": "credential_missing"}

    workspace_root = Path(args.workspace)
    workspace_root.mkdir(parents=True, exist_ok=True)
    _write_files(workspace_root, task["files"])
    before = _tree_hash(workspace_root)
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model=DEFAULT_MODEL,
        base_url=DEFAULT_BASE_URL,
        max_output_tokens=args.max_output_tokens,
        context_window_tokens=32768,
        input_price_per_million=INPUT_RATE,
        output_price_per_million=OUTPUT_RATE,
    )
    profile_data = profile.model_dump(mode="json")
    profile_hash = hashlib.sha256(json.dumps(profile_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    budget = BudgetFile(args.budget_file, args.budget_usd)
    trial_id = args.trial_id
    transport = BudgetTransport(_provider_transport(), budget, trial_id, DEFAULT_MODEL)
    client = httpx.AsyncClient(transport=transport, trust_env=False)
    db_path = Path(args.data_dir) / "ling.db"
    app = Application(db_path, credential_cache=False)
    runtime = None
    runtime_closed = False
    key_cleared = False
    try:
        agent = app.create_agent("编程评测")
        model = app.profiles.save("评测模型", profile, api_key=key)
        app.profiles.select(model["id"], None)
        app.permissions.configure(agent, readable=[workspace_root], writable=[workspace_root],
                                  expected_revision=0)
        limits = ExecutionLimits(timeout_seconds=120, max_output_bytes=262_144)
        runtime = TaskRuntime(app, client=client, max_steps=40, limits=limits)
        await runtime.start()
        objective = task["objective"]
        tid = await runtime.submit({
            "agent_id": agent,
            "objective": objective,
            "request_id": f"coding-eval-{trial_id}",
            "workspace": str(workspace_root),
            "environment": "python",
        })
        outcome = await _wait_for_task(runtime, tid, task, args.task_timeout_seconds)
        task_record = runtime.get(tid)
        try:
            diff = runtime.changes(tid)
        except (KeyError, RuntimeError, ValueError):
            diff = {"files": []}
        original_unchanged_after_model = _tree_hash(workspace_root) == before
        delivery_conflict = None
        if task["id"] == "slow_delivery":
            review = runtime.changes(tid)
            if review["review_id"]:
                delivery_conflict = await _probe_delivery_conflict(
                    runtime, workspace_root, tid, review["review_id"]
                )
            else:
                delivery_conflict = {"status": "framework_failure", "error_code": "review_missing"}
        acceptance = _run_acceptance(app, runtime.workspace(tid), task["id"])
        events = runtime.events(tid, limit=1000)
        usage_by_step = {}
        usage_rows = []
        tool_rows = []
        for event in events:
            data = event["data"]
            if event["type"] == "usage":
                usage = data.get("usage")
                if usage is not None:
                    usage_by_step[data.get("step", 0)] = usage
                    usage_rows.append({"step": data.get("step"), "usage": usage})
            elif event["type"] == "tool_result":
                result = data.get("result") or {}
                tool_rows.append({
                    "name": data.get("name"),
                    "status": result.get("status"),
                    "error_code": result.get("error_code"),
                    "exit_code": result.get("exit_code"),
                    "timed_out": result.get("timed_out", False),
                    "output_truncated": result.get("output_truncated", False),
                    "execution_purpose": result.get("execution_purpose"),
                    "stdout": _redact(str(result.get("stdout", ""))[:2048], key),
                    "stderr": _redact(str(result.get("stderr", ""))[:2048], key),
                })
        budget.finalize_trial(trial_id, usage_by_step)
        steps = _read_steps(app, tid, key)
        commands = _command_durations(events)
        slow_ok = task["id"] != "slow_delivery" or any(
            command["duration_seconds"] >= 30 for command in commands
        )
        budget_limited = transport.budget_limited
        if budget_limited:
            status = "budget_limited"
        elif (
            outcome.get("error_code") == "task_timeout"
            or (task["id"] == "guided_recovery" and not outcome.get("guidance_sent"))
            or (delivery_conflict and delivery_conflict.get("status") != "passed")
            or not original_unchanged_after_model
        ):
            status = "framework_failure"
        elif not acceptance["passed"] or not slow_ok:
            status = "model_not_solved"
        elif task_record["status"] in {"completed", "needs_review"}:
            status = "passed"
        else:
            status = _classify_failure(task_record, tool_rows)
        database_bytes = db_path.read_bytes() if db_path.exists() else b""
        record = {
            "status": status,
            "case_id": task["id"],
            "task": {key_name: task_record.get(key_name) for key_name in
                     ("id", "status", "verified", "step_count", "error_code", "final")},
            "guidance_sent": outcome.get("guidance_sent", False),
            "acceptance": acceptance,
            "delivery_conflict_probe": delivery_conflict,
            "slow_command": {"observed_over_30_seconds": slow_ok, "commands": commands},
            "profile": {"model": DEFAULT_MODEL, "base_url": DEFAULT_BASE_URL,
                        "profile_hash": profile_hash},
            "usage": usage_rows,
            "budget": budget.snapshot(trial_id),
            "tool_results": tool_rows,
            "steps": steps,
            "diff": [{key_name: file.get(key_name) for key_name in
                      ("path", "operation", "before", "after", "diff")} for file in diff.get("files", [])],
            "resource_cleanup": {
                "task_runtime_closed": False,
                "app_closed": False,
                "credential_absent_from_database": key.encode() not in database_bytes,
            },
            "original_workspace_unchanged_after_model": original_unchanged_after_model,
        }
    finally:
        if runtime is not None:
            try:
                await runtime.aclose()
                runtime_closed = True
            except (RuntimeError, OSError):
                pass
        try:
            await client.aclose()
        except (RuntimeError, httpx.HTTPError):
            pass
        app.profiles.clear_credentials()
        app.close()
        key_cleared = True
    record["resource_cleanup"]["task_runtime_closed"] = runtime_closed
    record["resource_cleanup"]["app_closed"] = True
    record["resource_cleanup"]["credential_cleared_from_memory"] = key_cleared
    return _redact_value(record, key)


async def _wait_for_task(runtime, task_id: str, task: dict, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    cursor = 0
    guidance_sent = False
    guidance_pending = task.get("owner_guidance")
    while time.monotonic() < deadline:
        events = runtime.events(task_id, after=cursor, limit=200)
        for event in events:
            cursor = event["id"]
            if guidance_pending and not guidance_sent and event["type"] == "tool_result":
                try:
                    await runtime.steer(task_id, guidance_pending)
                    guidance_sent = True
                    guidance_pending = None
                except (RuntimeError, ValueError):
                    guidance_sent = False
        current = runtime.get(task_id)
        if current["status"] not in {"running", "queued"}:
            return {"status": current["status"], "guidance_sent": guidance_sent}
        await asyncio.sleep(0.1)
    await runtime.cancel(task_id)
    return {"status": runtime.get(task_id)["status"], "error_code": "task_timeout",
            "guidance_sent": guidance_sent}


def _run_acceptance(app, workspace: Path, case_id: str) -> dict:
    isolation_level = "unknown"
    try:
        isolation_level = app.sandbox.level()
        limits = _execution_limits()
        executor = app.sandbox.executor(
            workspace,
            image=app.sandbox.image,
            writable=False,
            limits=limits,
            network_mode="none",
        )
        python = "python" if app.sandbox.level() == "container" else sys.executable
        result = executor.execute([python, "-I", "-c", _VERIFIER, case_id])
        return {"passed": result.status == "success" and result.exit_code == 0,
                "status": result.status, "exit_code": result.exit_code,
                "timed_out": result.timed_out, "error_code": result.error,
                "isolation_level": isolation_level,
                "workspace_read_only_enforced": isolation_level == "container",
                "verifier_boundary": "container_read_only" if isolation_level == "container"
                else "local_best_effort_not_confidential"}
    except (OSError, RuntimeError, ValueError, ImportError, TypeError):
        return {
            "passed": False,
            "status": "unknown",
            "error_code": "verifier_failed",
            "isolation_level": isolation_level,
            "workspace_read_only_enforced": isolation_level == "container",
            "verifier_boundary": "local_best_effort_not_confidential"
            if isolation_level == "local" else "unknown",
        }


def _execution_limits():
    from lingnexus.tools import ExecutionLimits
    return ExecutionLimits(timeout_seconds=15, max_output_bytes=8192)


def _read_steps(app, task_id: str, key: str) -> list[dict]:
    with app.database.transaction() as db:
        rows = db.execute(
            "SELECT model_step,name,arguments,status,result FROM task_steps "
            "WHERE task_id=? ORDER BY model_step,rowid", (task_id,),
        ).fetchall()
    values = []
    for row in rows:
        try:
            arguments = json.loads(row["arguments"])
            result = json.loads(row["result"]) if row["result"] else None
        except (TypeError, ValueError):
            arguments, result = {}, None
        values.append({
            "model_step": row["model_step"], "name": row["name"], "status": row["status"],
            "arguments": _redact_value(arguments, key),
            "result": _tool_summary(result, key),
        })
    return values


def _tool_summary(result: Any, key: str) -> dict | None:
    if not isinstance(result, dict):
        return None
    names = ("status", "error_code", "exit_code", "timed_out", "output_truncated",
             "verified", "verification_passed", "execution_purpose", "workspace_version")
    summary = {name: result[name] for name in names if name in result}
    for channel in ("stdout", "stderr"):
        if isinstance(result.get(channel), str):
            summary[channel] = _redact(result[channel][:2048], key)
    return summary


def _command_durations(events: list[dict]) -> list[dict]:
    starts = {}
    durations = []
    for event in events:
        data = event.get("data") or {}
        if event["type"] == "tool_started" and data.get("name") == "run_command":
            starts[data.get("step_id")] = event.get("created_at")
        elif event["type"] == "tool_result" and data.get("name") == "run_command":
            start = starts.get(data.get("step_id"))
            try:
                duration = (datetime.fromisoformat(event["created_at"])
                            - datetime.fromisoformat(start)).total_seconds()
            except (TypeError, ValueError, KeyError):
                continue
            durations.append({"step_id": data.get("step_id"),
                              "duration_seconds": round(max(0, duration), 3),
                              "status": (data.get("result") or {}).get("status")})
    return durations


def _classify_failure(task: dict, tool_rows: list[dict]) -> str:
    code = task.get("error_code") or ""
    if code in {"credentials_required", "permissions_revoked", "task_state_invalid",
                "task_runtime_error", "profile_changed", "snapshot_invalid"}:
        return "framework_failure"
    if code.startswith(("model_transport", "model_http")) or code == "model_timeout":
        return "framework_failure"
    if any(item.get("error_code") in {"container_unavailable", "resource_limits_unavailable"}
           for item in tool_rows):
        return "environment_missing"
    return "model_not_solved"


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _redact(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


def _redact_value(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return _redact(value, key)
    if isinstance(value, list):
        return [_redact_value(item, key) for item in value]
    if isinstance(value, dict):
        return {name: _redact_value(item, key) for name, item in value.items()}
    return value


def _resolve_product_source(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if (path / "src" / "lingnexus" / "app.py").is_file():
        return path
    if (path / "lingnexus" / "app.py").is_file() and path.name == "src":
        return path.parent
    raise ValueError("product_source_invalid")


def _provider_transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(
        proxy=os.environ.get("LINGNEXUS_EVAL_PROXY") or None,
        trust_env=False,
    )


async def _probe_delivery_conflict(runtime, workspace: Path, task_id: str,
                                   review_id: str) -> dict:
    target = workspace / "release.py"
    try:
        original = target.read_bytes()
        marker = b"\n# intentional controller conflict probe\n"
        target.write_bytes(original + marker)
        changed = target.read_bytes()
        try:
            await runtime.apply(task_id, review_id=review_id)
        except Exception as exc:  # noqa: BLE001 - record only the safe outcome category.
            from lingnexus.common.errors import Conflict
            rejected = isinstance(exc, Conflict)
        else:
            rejected = False
        preserved = target.read_bytes() == changed
        passed = rejected and preserved
        return {
            "status": "passed" if passed else "framework_failure",
            "rejected_stale_review": rejected,
            "controller_change_preserved": preserved,
            "control_mutation_excluded_from_model_workspace_check": True,
        }
    except OSError:
        return {"status": "framework_failure", "error_code": "conflict_probe_io_failed"}


def _source_identity(source: Path) -> dict:
    source = source.resolve()
    revision = "unknown"
    try:
        top = subprocess.run(["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                             check=False, text=True, encoding="utf-8", errors="replace")
        if top.returncode == 0:
            repository_root = Path(top.stdout.strip()).resolve()
            if repository_root == source:
                head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                      stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                                      check=False, text=True, encoding="utf-8", errors="replace")
                if head.returncode == 0:
                    revision = head.stdout.strip()
            else:
                revision = "export/unknown"
    except (OSError, subprocess.TimeoutExpired):
        pass
    digest = hashlib.sha256()
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".artifacts",
               ".tmp", ".deps", ".home"}
    lock_names = {"uv.lock", "poetry.lock", "pdm.lock", "pipfile.lock",
                  "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements.lock"}
    files = []
    for path in source.rglob("*"):
        if (not path.is_file() or path.is_symlink()
                or any(part.lower() in ignored for part in path.relative_to(source).parts)):
            continue
        if path.suffix.lower() in {".py", ".sql", ".lock"} or path.name.lower() in lock_names:
            files.append(path)
    for file in sorted(files):
        digest.update(file.relative_to(source).as_posix().encode("utf-8"))
        digest.update(file.read_bytes())
    return {"revision": revision, "source_hash": digest.hexdigest()}


def _lock_and_write_result(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _launch_trial(args, product_source: Path, task: dict, repeat: int,
                  budget_file: Path, output_file: Path, source_identity: dict) -> dict:
    trial_id = f"{task['id']}-r{repeat}-{uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="ling-coding-trial-") as temp:
        trial_dir = Path(temp)
        workspace = trial_dir / "workspace"
        data_dir = trial_dir / "data"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(product_source / "src")
        command = [
            sys.executable, str(SCRIPT), "--internal-trial",
            "--product-source", str(product_source),
            "--dataset", str(args.dataset),
            "--case-id", task["id"],
            "--trial-id", trial_id,
            "--workspace", str(workspace),
            "--data-dir", str(data_dir),
            "--budget-file", str(budget_file),
            "--budget-usd", str(args.budget_usd),
            "--max-output-tokens", str(args.max_output_tokens),
            "--task-timeout-seconds", str(args.task_timeout_seconds),
        ]
        started_at = _utc_now()
        try:
            child = subprocess.run(command, cwd=product_source, env=env,
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=args.task_timeout_seconds + 90,
                                   check=False, text=True, encoding="utf-8", errors="replace")
            if child.returncode:
                child_result = {"status": "framework_failure", "error_code": "trial_worker_failed"}
            else:
                child_result = json.loads(child.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            child_result = {"status": "interrupted", "error_code": "trial_process_timeout"}
        except (OSError, ValueError, IndexError):
            child_result = {"status": "framework_failure", "error_code": "trial_worker_failed"}
    record = {
        "evaluation_version": 1,
        "dataset_version": DATASET_VERSION,
        "attempt_id": trial_id,
        "case_id": task["id"],
        "category": task["category"],
        "repeat": repeat,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "product_source": source_identity,
        "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL,
        "max_output_tokens": args.max_output_tokens,
        **child_result,
    }
    _lock_and_write_result(output_file, record)
    return record


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def run_controller(args) -> dict:
    dataset = load_dataset(args.dataset)
    source = _resolve_product_source(args.product_source)
    identity = _source_identity(source)
    api_key = os.environ.get("LINGNEXUS_EVAL_API_KEY", "")
    if not api_key:
        raise ValueError("credential_missing")
    budget_file = Path(args.budget_file)
    BudgetFile(budget_file, args.budget_usd)
    args.dataset = Path(args.dataset).resolve()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_tag = args.run_label or identity["revision"][:12] or "source"
    jsonl = output_dir / f"coding-eval-{run_tag}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}.jsonl"
    selected = [item for item in dataset["tasks"] if not args.task_id or item["id"] in args.task_id]
    records = []
    try:
        for repeat in range(1, args.repeat + 1):
            for task in selected:
                records.append(_launch_trial(args, source, task, repeat, budget_file, jsonl, identity))
    except KeyboardInterrupt:
        _lock_and_write_result(jsonl, {
            "evaluation_version": 1, "dataset_version": DATASET_VERSION,
            "status": "interrupted", "error_code": "owner_interrupted", "finished_at": _utc_now(),
        })
        raise
    snapshot = BudgetFile(budget_file, args.budget_usd).snapshot()
    passed = sum(record.get("status") == "passed" for record in records)
    counts = {status: sum(record.get("status") == status for record in records)
              for status in ("passed", "model_not_solved", "framework_failure",
                             "environment_missing", "budget_limited", "interrupted")}
    summary = {
        "dataset_version": DATASET_VERSION,
        "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL,
        "profile_hash": _profile_hash(args.max_output_tokens),
        "product_source": identity,
        "planned": len(selected) * args.repeat,
        "attempted": len(records),
        "passed": passed,
        "counts": counts,
        "budget": snapshot,
        "attempts_file": str(jsonl),
        "budget_file": str(budget_file),
    }
    summary_path = output_dir / (jsonl.stem + "-summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _profile_hash(max_output_tokens: int) -> str:
    config = {
        "api_family": "openai_chat_completions", "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL, "max_output_tokens": max_output_tokens,
        "context_window_tokens": 32768,
        "input_price_per_million": str(INPUT_RATE),
        "output_price_per_million": str(OUTPUT_RATE),
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate fixtures offline; never call a model")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--product-source", type=Path, default=REPOSITORY)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--task-id", action="append", help="limit real attempts to a fixture id; repeatable")
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--task-timeout-seconds", type=int, default=1200)
    parser.add_argument("--budget-usd", type=Decimal, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--budget-file", type=Path, default=DEFAULT_BUDGET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-label", default="")
    parser.add_argument("--internal-trial", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--case-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--trial-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, default=Path("."), help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=Path("."), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10 or not 1 <= args.max_output_tokens <= 1024:
        parser.error("repeat must be 1..10 and max-output-tokens must be 1..1024")
    if args.task_timeout_seconds < 1:
        parser.error("task-timeout-seconds must be positive")
    if args.task_id:
        valid = {task["id"] for task in load_dataset(args.dataset)["tasks"]}
        if any(case not in valid for case in args.task_id):
            parser.error("unknown task id")
    return args


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        if args.internal_trial:
            try:
                result = asyncio.run(_trial_child(args))
            except (ImportError, AttributeError, TypeError):
                result = {"status": "framework_failure", "error_code": "product_source_unsupported"}
            except Exception:  # noqa: BLE001 - prevent credentials from entering worker output.
                result = {"status": "framework_failure", "error_code": "trial_worker_failed"}
        elif args.dry_run:
            result = dry_run(args.dataset)
        else:
            result = run_controller(args)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result.get("status", "passed") in {"passed", "success"} else 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "error_code": "owner_interrupted"}), file=sys.stderr)
        return 130
    except ValueError as exc:
        code = "credential_missing" if str(exc) == "credential_missing" else "evaluation_failed"
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never print provider or child exception text.
        # Exception messages and tracebacks can contain provider credentials.
        print(json.dumps({"status": "failed", "error_code": "evaluation_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
