"""Acceptance tests for the two-tier execution sandbox.

The default tier must run programming work with no container engine present, and
every protection it claims must be a real, testable guarantee:

* the local tier runs an argument list with no shell inside the task work copy,
* time, output, process-count, memory and CPU-time bounds really stop a command,
* nothing from the command's process tree survives the run,
* the tier choice is durable, the engine is probed but never auto-selected, and
  the capability statement never claims a guarantee the host cannot enforce.

Cases that depend on the Windows Job Object (memory/CPU/process-count/cleanup)
skip with an explicit reason where the host cannot enforce them, rather than
passing vacuously. Every other case asserts the real failure mode.
"""

import asyncio
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from lingnexus.common.errors import PermissionDenied
from lingnexus.tools import (
    ExecutionLimits,
    FileAccessDenied,
    FilePermissions,
    LocalLimits,
    RestrictedLocalSandbox,
    WorkspaceTools,
    create_executor,
    describe,
    local_environment,
    probe_engine,
    resource_limits_available,
)

# A baseline that cannot itself be the cause of a failure; each case narrows one
# bound so a red test points at exactly one protection.
_BASE_LIMITS = {
    "timeout_seconds": 20.0,
    "memory_bytes": 512 * 1024 * 1024,
    "cpu_seconds": 60.0,
    "pids_limit": 64,
    "max_output_bytes": 65_536,
}

JOB_LIMITS_REQUIRED = pytest.mark.skipif(
    not resource_limits_available(),
    reason="本机没有可用的 Windows 作业对象，进程数/内存/CPU 时间上限无法强制",
)


def limits(**overrides) -> LocalLimits:
    return LocalLimits(**{**_BASE_LIMITS, **overrides})


def local(worktree, **overrides) -> RestrictedLocalSandbox:
    return RestrictedLocalSandbox(worktree, writable=True, limits=limits(**overrides))


def worktree(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


# --- Windows process liveness (bounded wait, never a sleep) ------------------

_SYNCHRONIZE = 0x00100000


def wait_process_gone(pid: int, timeout_ms: int) -> bool:
    """True once the process has exited; waits at most timeout_ms, never sleeps."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return True  # already gone; the handle cannot be opened
    try:
        return kernel.WaitForSingleObject(handle, timeout_ms) == 0
    finally:
        kernel.CloseHandle(handle)


# --- 1. the default tier runs without any container engine -------------------


def test_default_tier_runs_a_command_without_any_engine(tmp_path):
    root = worktree(tmp_path)
    runner = create_executor("local", root, writable=True, local_limits=limits())
    assert runner.level == "local"
    result = runner.execute([sys.executable, "-c", "print('default tier ran')"])
    assert result.status == "success", result
    assert result.exit_code == 0
    assert "default tier ran" in result.stdout
    assert result.cleanup_confirmed is True


def test_sandbox_service_executor_is_local_by_default(app, tmp_path):
    root = worktree(tmp_path)
    assert app.sandbox.level() == "local"
    runner = app.sandbox.executor(root, limits=ExecutionLimits(timeout_seconds=20))
    assert runner.level == "local"
    result = runner.execute([sys.executable, "-c", "print('via service')"])
    assert result.status == "success", result
    assert "via service" in result.stdout


def test_coding_task_runs_a_command_without_a_container(app, agent_id, tmp_path):
    """Acceptance 1: a programming task completes on the default tier alone."""
    from lingnexus.tasks import TaskRuntime

    configure(app)
    project = tmp_path / "project"
    project.mkdir()
    (project / "check.py").write_text("print('local tier ran the task')\n", encoding="utf-8")
    app.permissions.configure(
        agent_id, readable=[project], writable=[project], expected_revision=0
    )

    async def run():
        replies = [
            tool_reply(
                "Run the check.",
                [
                    (
                        "run-1",
                        "run_command",
                        {"argv": [sys.executable, "check.py"], "purpose": "verify"},
                    )
                ],
            ),
            tool_reply("Verified."),
        ]
        async with mock_client(replies) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {
                    "agent_id": agent_id,
                    "objective": "run check.py",
                    "request_id": "local-tier-task",
                    "workspace": str(project),
                }
            )
            state = await settle(runtime, tid)
            events = runtime.events(tid)
            await runtime.close()
            return state, events

    state, events = asyncio.run(run())
    assert state["status"] == "completed", state
    assert state["verified"] is True, state
    results = [
        event
        for event in events
        if event["type"] == "tool_result" and event["data"]["name"] == "run_command"
    ]
    assert results, events
    executed = results[0]["data"]["result"]
    assert executed["status"] == "success", executed
    assert executed["cleanup_confirmed"] is True
    assert "local tier ran the task" in executed["stdout"]


# --- 2. directory restriction ------------------------------------------------


def test_file_tools_refuse_paths_outside_the_grant(tmp_path):
    granted = worktree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tools = WorkspaceTools(granted, FilePermissions((granted,), (granted,)))

    with pytest.raises(FileAccessDenied):
        tools.read_bytes(outside)
    with pytest.raises(FileAccessDenied):
        tools.read_bytes(granted / ".." / "outside.txt")
    with pytest.raises(FileAccessDenied):
        tools.create_bytes(tmp_path / "new.txt", b"nope")
    assert not (tmp_path / "new.txt").exists()


def test_owner_file_access_refuses_paths_outside_the_grant(app, agent_id, tmp_path):
    granted = worktree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    app.permissions.configure(
        agent_id, readable=[granted], writable=[granted], expected_revision=0
    )
    with pytest.raises(PermissionDenied):
        app.permissions.read_text(agent_id, outside)
    with pytest.raises(PermissionDenied):
        app.permissions.write_text(agent_id, outside, "overwritten")
    assert outside.read_text(encoding="utf-8") == "secret"


def test_command_runs_inside_the_worktree(tmp_path):
    root = worktree(tmp_path)
    result = local(root).execute(
        [
            sys.executable,
            "-c",
            "import os, pathlib; pathlib.Path('made.txt').write_text(os.getcwd()); print(os.getcwd())",
        ]
    )
    assert result.status == "success", result
    assert (root / "made.txt").exists()
    assert Path(result.stdout.strip()) == root


def test_child_environment_does_not_leak_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LINGNEXUS_TEST_SECRET", "leak-me")
    environment = local_environment(tmp_path)
    assert "LINGNEXUS_TEST_SECRET" not in environment
    assert environment["TMP"] == str(tmp_path / ".tmp")
    assert environment["HOME"] == str(tmp_path / ".home")
    result = local(tmp_path).execute(
        [sys.executable, "-c", "import os; print(os.environ.get('LINGNEXUS_TEST_SECRET'))"]
    )
    assert result.status == "success", result
    assert result.stdout.strip() == "None"


# --- 3. no shell -------------------------------------------------------------


def test_arguments_are_passed_without_a_shell(tmp_path):
    root = worktree(tmp_path)
    metacharacters = "one; two && three | four > redirected.txt"
    result = local(root).execute(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", metacharacters]
    )
    assert result.status == "success", result
    # The argument arrives verbatim: no metacharacter was interpreted.
    assert result.stdout.strip() == metacharacters
    # A shell would have performed the redirection and created this file.
    assert not (root / "redirected.txt").exists()


def test_string_commands_and_option_style_executables_are_rejected(tmp_path):
    root = worktree(tmp_path)
    runner = local(root)
    with pytest.raises(ValueError):
        runner.execute("echo hello")
    with pytest.raises(ValueError):
        runner.execute([])
    with pytest.raises(ValueError):
        runner.execute(["--version"])
    with pytest.raises(ValueError):
        runner.execute([sys.executable, "arg\0with-nul"])


# --- 4. timeout --------------------------------------------------------------


def test_timeout_terminates_a_hanging_command(tmp_path):
    root = worktree(tmp_path)
    result = local(root, timeout_seconds=1.0).execute(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    assert result.timed_out is True, result
    assert result.status == "timeout", result
    assert result.error == "execution_timed_out"


# --- 5. output bound ---------------------------------------------------------


def test_output_is_truncated_at_the_limit(tmp_path):
    root = worktree(tmp_path)
    result = local(root, max_output_bytes=100).execute(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"]
    )
    assert result.output_truncated is True, result
    assert len(result.stdout) <= 100, len(result.stdout)


# --- 6. memory bound ---------------------------------------------------------


@JOB_LIMITS_REQUIRED
def test_memory_limit_stops_an_oversized_allocation(tmp_path):
    root = worktree(tmp_path)
    code = (
        "import sys\n"
        "print('STARTED', flush=True)\n"
        "data = bytearray(1024 * 1024 * 1024)\n"
        "print('ALLOCATED', flush=True)\n"
    )
    result = local(root, memory_bytes=256 * 1024 * 1024).execute([sys.executable, "-c", code])
    # The interpreter started, so the missing marker is the failed allocation.
    assert "STARTED" in result.stdout, result
    assert "ALLOCATED" not in result.stdout, result
    assert result.status != "success", result
    assert result.exit_code not in (0, None), result


# --- 7. CPU-time bound -------------------------------------------------------


@JOB_LIMITS_REQUIRED
def test_cpu_time_limit_terminates_a_busy_loop(tmp_path):
    root = worktree(tmp_path)
    result = local(root, cpu_seconds=1.0, timeout_seconds=20.0).execute(
        [sys.executable, "-c", "while True: pass"]
    )
    # Not the wall-clock timeout: the CPU-time limit stopped it first.
    assert result.timed_out is False, result
    assert result.status != "success", result
    assert result.exit_code not in (0, None), result


# --- 8. process-count bound --------------------------------------------------


@JOB_LIMITS_REQUIRED
def test_process_count_limit_blocks_new_processes(tmp_path):
    """A tight cap refuses further process creation with ERROR_NOT_ENOUGH_QUOTA.

    The cap counts every active process in the job, and Windows also attaches a
    console host to some children, so the exact number that slips through is not
    fixed. The bound that is guaranteed — and asserted — is that no more than
    ``limit`` long-lived children can be created, with the rest refused by the
    job (winerror 1816); the unconstrained control run creates them all.
    """
    root = worktree(tmp_path)
    attempts = 8
    code = (
        "import subprocess, sys\n"
        "spawned = 0\n"
        "quota_errors = 0\n"
        f"for _ in range({attempts}):\n"
        "    try:\n"
        "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "        spawned += 1\n"
        "    except OSError as error:\n"
        "        quota_errors += 1 if error.winerror == 1816 else 0\n"
        "print('SPAWNED', spawned)\n"
        "print('QUOTA', quota_errors)\n"
    )
    tight = local(root, pids_limit=4).execute([sys.executable, "-c", code])
    assert tight.status == "success", tight
    spawned = int(tight.stdout.split("SPAWNED")[1].split()[0])
    quota = int(tight.stdout.split("QUOTA")[1].split()[0])
    assert spawned <= 4, tight.stdout
    assert spawned < attempts, tight.stdout
    assert quota >= 1, tight.stdout

    control = local(root, pids_limit=64).execute([sys.executable, "-c", code])
    assert control.status == "success", control
    assert int(control.stdout.split("SPAWNED")[1].split()[0]) == attempts, control.stdout


# --- 9. no surviving child process -------------------------------------------


@JOB_LIMITS_REQUIRED
def test_no_child_process_survives_the_run(tmp_path):
    root = worktree(tmp_path)
    pidfile = root / "child.pid"
    child_code = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    parent_code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(1.0)\n"
        "print('PARENT_DONE')\n"
    )
    result = local(root).execute([sys.executable, "-c", parent_code])
    assert result.status == "success", result
    assert "PARENT_DONE" in result.stdout
    assert pidfile.exists(), "the grandchild never started; the case proved nothing"
    child = int(pidfile.read_text(encoding="utf-8"))
    assert wait_process_gone(child, 5000), "a child process outlived the run"


# --- 10. durable tier selection ----------------------------------------------


def test_level_defaults_to_local_and_persists_across_restart(app_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lingnexus.tasks.sandbox.probe_engine",
        lambda environ=None: {"available": True, "name": "fixture-engine"},
    )
    database = tmp_path / "ling.db"
    first = app_factory(database)
    try:
        assert first.sandbox.level() == "local"
        first.sandbox.set_level("container")
        assert first.sandbox.level() == "container"
    finally:
        first.close()
    second = app_factory(database)
    try:
        assert second.sandbox.level() == "container"
    finally:
        second.close()


def test_container_level_requires_an_available_engine(app, monkeypatch):
    monkeypatch.setattr(
        "lingnexus.tasks.sandbox.probe_engine",
        lambda environ=None: {"available": False, "name": None},
    )
    with pytest.raises(PermissionDenied):
        app.sandbox.set_level("container")
    assert app.sandbox.level() == "local"


def test_unknown_level_is_rejected(app):
    with pytest.raises(ValueError):
        app.sandbox.set_level("vm")


def test_local_limits_validate_their_bounds():
    for bad in (
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"cpu_seconds": 0},
        {"memory_bytes": 0},
        {"pids_limit": 0},
        {"max_output_bytes": 0},
    ):
        with pytest.raises(ValueError):
            LocalLimits(**bad)


# --- 11. interface shape -----------------------------------------------------

SHAPE = {"level", "available_levels", "engine", "protections", "limitations", "image"}


def test_sandbox_status_shape(client, headers):
    response = client.get("/sandbox", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SHAPE
    assert body["level"] == "local"
    assert body["available_levels"][0] == "local"
    assert set(body["available_levels"]) <= {"local", "container"}
    assert set(body["engine"]) == {"available", "name"}
    assert isinstance(body["engine"]["available"], bool)
    assert isinstance(body["protections"], list) and body["protections"]
    assert isinstance(body["limitations"], list) and body["limitations"]
    assert isinstance(body["image"], str) and body["image"]


def test_sandbox_put_local_round_trip(client, headers):
    response = client.put("/sandbox", headers=headers, json={"level": "local"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SHAPE
    assert body["level"] == "local"
    assert client.get("/sandbox", headers=headers).json()["level"] == "local"


def test_sandbox_put_container_without_engine_is_denied(client, headers, monkeypatch):
    monkeypatch.setattr(
        "lingnexus.tasks.sandbox.probe_engine",
        lambda environ=None: {"available": False, "name": None},
    )
    response = client.put("/sandbox", headers=headers, json={"level": "container"})
    assert response.status_code == 403
    assert client.get("/sandbox", headers=headers).json()["level"] == "local"


def test_sandbox_put_invalid_level_is_rejected(client, headers):
    assert client.put("/sandbox", headers=headers, json={"level": "vm"}).status_code == 422
    assert client.get("/sandbox").status_code == 401


# --- 12. honest capability statement -----------------------------------------


def test_local_statement_is_honest_about_job_limits():
    enforced = describe("local", job_limits=True)
    degraded = describe("local", job_limits=False)
    assert enforced["limitations"] and degraded["limitations"]

    resource_claims = "进程数"
    assert any(resource_claims in item for item in enforced["protections"])
    assert not any(resource_claims in item for item in degraded["protections"])
    assert any("作业对象" in item for item in degraded["limitations"])
    assert not any("作业对象" in item for item in enforced["limitations"])

    # The local tier must never claim filesystem or network isolation.
    for statement in (enforced, degraded):
        assert not any("文件系统隔离" in item for item in statement["protections"])
        assert any("网络隔离" in item for item in statement["limitations"])


def test_service_reports_missing_job_limits_without_claiming_them(app, monkeypatch):
    monkeypatch.setattr("lingnexus.tasks.sandbox.resource_limits_available", lambda: False)
    info = app.sandbox.info()
    assert info["level"] == "local"
    assert info["limitations"]
    assert any("作业对象" in item for item in info["limitations"])
    assert not any("进程数" in item for item in info["protections"])


# --- 13. engine probing never switches the tier ------------------------------


def test_engine_probe_reports_unavailable_without_binaries():
    assert probe_engine({"PATH": ""}) == {"available": False, "name": None}


def test_engine_availability_never_switches_the_tier(app, monkeypatch):
    monkeypatch.setattr(
        "lingnexus.tasks.sandbox.probe_engine",
        lambda environ=None: {"available": False, "name": None},
    )
    assert app.sandbox.engine()["available"] is False
    assert app.sandbox.available_levels() == ["local"]
    assert app.sandbox.level() == "local"

    # An engine appearing later makes the container tier *available*, not chosen.
    monkeypatch.setattr(
        "lingnexus.tasks.sandbox.probe_engine",
        lambda environ=None: {"available": True, "name": "fixture-engine"},
    )
    app.sandbox.probe()
    assert app.sandbox.available_levels() == ["local", "container"]
    assert app.sandbox.level() == "local"
    assert app.sandbox.engine() == {"available": True, "name": "fixture-engine"}


# --- shared task/model helpers ----------------------------------------------


def configure(app, **limits_overrides):
    from lingnexus.models import APIFamily, ModelProfile

    item = app.profiles.save(
        "fixture",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="fixture",
            base_url="https://fixture.test",
            **limits_overrides,
        ),
        api_key="runtime-secret-only",
    )
    app.profiles.select(item["id"], None)
    return item["id"]


def tool_reply(text=None, calls=()):
    import json

    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {
                "index": index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (call_id, name, arguments) in enumerate(calls)
        ]
    frame = {
        "id": "chat-native",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def mock_client(responses):
    import httpx

    def handler(request):
        value = responses.pop(0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=value)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def settle(runtime, tid):
    for _ in range(2000):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            return runtime.get(tid)
        await asyncio.sleep(0.01)
    raise AssertionError("task worker did not reach a checkpoint")
