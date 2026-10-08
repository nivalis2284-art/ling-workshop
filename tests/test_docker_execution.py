"""Unit tests fake Popen only; smoke is explicit and never downloads an image."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

IMAGE_ID = "sha256:" + "a" * 64


class Process:
    def __init__(self, args, *, stdout=b"", stderr=b"", code=0, timeout=False):
        self.args = args
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self.code = code
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = -9 if self.killed else self.code
        return self.returncode

    def kill(self):
        self.killed = True


class DockerBoundary:
    def __init__(self):
        self.calls = []
        self.processes = []
        self.state = {"Status": "exited", "Running": False, "ExitCode": 0,
                      "OOMKilled": False, "Error": ""}
        self.metadata = {"os": "linux", "id": IMAGE_ID, "volumes": None}
        self.overrides = {}

    def __call__(self, args, **kwargs):
        assert isinstance(args, list)
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        self.calls.append(args)
        action = args[1]
        defaults = {}
        if action == "image":
            defaults["stdout"] = json.dumps(self.metadata).encode()
        elif action == "inspect":
            defaults["stdout"] = json.dumps(self.state).encode()
        elif action == "start":
            defaults.update(stdout=b"fixture stdout", stderr=b"fixture stderr")
        settings = self.overrides.get(action, {})
        if isinstance(settings, BaseException):
            raise settings
        process = Process(args, **(defaults | settings))
        self.processes.append(process)
        return process


@pytest.fixture
def boundary(monkeypatch):
    fake = DockerBoundary()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return fake


def executor(tmp_path, **kwargs):
    from lingnexus.tools import DockerExecutor

    return DockerExecutor(tmp_path, image="fixture:approved", **kwargs)


def test_container_contract_and_literal_arguments(boundary, tmp_path):
    result = executor(tmp_path).execute(["python", "-c", "print('ok')", "$(host); & x"])
    assert result.status == "success"
    assert result.exit_code == 0
    assert result.stdout == "fixture stdout"
    assert result.stderr == "fixture stderr"
    create = next(args for args in boundary.calls if args[1] == "create")
    options = {"--user": "65534:65534", "--network": "none",
               "--cap-drop": "ALL", "--security-opt": "no-new-privileges=true",
               "--memory": "268435456", "--memory-swap": "268435456",
               "--cpus": "1.0", "--pids-limit": "64", "--workdir": "/workspace",
               "--pull": "never", "--log-driver": "none", "--entrypoint": "python"}
    for flag, value in options.items():
        assert create[create.index(flag) + 1] == value
    assert "--read-only" in create and "--no-healthcheck" in create
    assert create.count("--mount") == 1
    mount = create[create.index("--mount") + 1]
    assert f"source={tmp_path}" in mount
    assert "target=/workspace" in mount and "readonly" in mount
    assert "bind-recursive=disabled" in mount
    assert create[-4:] == [IMAGE_ID, "-c", "print('ok')", "$(host); & x"]
    assert all(flag not in create for flag in ("--privileged", "--volume", "-v", "--env-file"))
    assert "docker.sock" not in " ".join(create)
    assert boundary.calls[-1][1:3] == ["rm", "--force"]
    assert result.cleanup_confirmed


def test_writable_mount_must_be_explicit(boundary, tmp_path):
    executor(tmp_path, writable=True).execute(["true"])
    create = next(args for args in boundary.calls if args[1] == "create")
    assert "readonly" not in create[create.index("--mount") + 1]
    assert "--read-only" in create


@pytest.mark.parametrize("exit_code", [1, 37, 125, 126, 127, 137])
def test_exit_code_comes_from_container_not_cli(boundary, tmp_path, exit_code):
    boundary.state["ExitCode"] = exit_code
    result = executor(tmp_path).execute(["false"])
    assert result.status == "failed"
    assert result.exit_code == exit_code


@pytest.mark.parametrize("exit_code", [23, 125, 126, 127, 137])
def test_attached_cli_may_return_the_verified_container_exit(boundary, tmp_path, exit_code):
    boundary.overrides["start"] = {"code": exit_code}
    boundary.state["ExitCode"] = exit_code
    result = executor(tmp_path).execute(["false"])
    assert result.status == "failed", result
    assert result.exit_code == exit_code
    assert result.cleanup_confirmed


@pytest.mark.parametrize("state", [
    {"Status": "running", "Running": True, "ExitCode": 0},
    {"Status": "created", "Running": False, "ExitCode": 0},
    {"Status": "exited", "Running": False, "ExitCode": "0"},
    {"Status": "exited", "Running": False, "ExitCode": True},
])
def test_unverified_state_never_reports_success(boundary, tmp_path, state):
    boundary.state = state
    result = executor(tmp_path).execute(["true"])
    assert result.status == "unknown" and result.exit_code is None


@pytest.mark.parametrize("action", ["create", "start"])
def test_timeout_kills_named_container_and_cleans_up(boundary, tmp_path, action):
    boundary.overrides[action] = {"timeout": True}
    boundary.state["ExitCode"] = 137
    result = executor(tmp_path).execute(["sleep", "100"])
    assert result.status == "timeout" and result.timed_out
    assert result.exit_code == 137
    create = next(args for args in boundary.calls if args[1] == "create")
    name = create[create.index("--name") + 1]
    assert ["docker", "kill", name] in boundary.calls
    assert ["docker", "rm", "--force", name] in boundary.calls
    assert any(process.killed for process in boundary.processes)


def test_output_is_bounded_across_both_streams(boundary, tmp_path):
    from lingnexus.tools import ExecutionLimits

    boundary.overrides["start"] = {"stdout": b"x" * 100000, "stderr": b"y" * 100000}
    result = executor(tmp_path, limits=ExecutionLimits(max_output_bytes=111)).execute(["noisy"])
    assert len(result.stdout.encode()) + len(result.stderr.encode()) == 111
    assert result.output_truncated
    assert result.status == "success"


@pytest.mark.parametrize("action,settings", [
    ("image", {"code": 1}), ("image", FileNotFoundError("docker missing")),
    ("image", {"stdout": b"garbage"}), ("create", {"code": 125}),
    ("start", {"code": 1}), ("inspect", {"code": 1}),
    ("inspect", {"stdout": b"garbage"}), ("rm", {"code": 1}),
])
def test_infrastructure_failure_is_never_success(boundary, tmp_path, action, settings):
    boundary.overrides[action] = settings
    result = executor(tmp_path).execute(["true"])
    assert result.status in ("unknown", "failed")
    assert result.error
    if action == "rm":
        assert not result.cleanup_confirmed


@pytest.mark.parametrize("metadata", [
    {"os": "windows", "id": IMAGE_ID, "volumes": None},
    {"os": "linux", "id": IMAGE_ID, "volumes": {"/extra": {}}},
    {"os": "linux", "id": "unverified", "volumes": None},
])
def test_image_must_be_local_linux_without_implicit_volumes(boundary, tmp_path, metadata):
    boundary.metadata = metadata
    result = executor(tmp_path).execute(["true"])
    assert result.status == "failed"
    assert not any(args[1] in ("create", "pull", "run") for args in boundary.calls)


@pytest.mark.parametrize("argv", [[], "echo hi", [""], ["--privileged"], ["true", "bad\x00arg"]])
def test_invalid_command_is_rejected_before_subprocess(boundary, tmp_path, argv):
    if isinstance(argv, list):
        argv = [arg.replace("\x00", "\x00") for arg in argv]
    with pytest.raises((ValueError, TypeError)):
        executor(tmp_path).execute(argv)
    assert not boundary.calls


@pytest.mark.parametrize("field,value", [
    ("timeout_seconds", 0), ("timeout_seconds", float("nan")),
    ("timeout_seconds", float("inf")), ("cpus", -1), ("cpus", True),
    ("memory_bytes", 0), ("pids_limit", 0), ("max_output_bytes", 0),
])
def test_resource_limits_cannot_disable_bounds(field, value):
    from lingnexus.tools import ExecutionLimits

    with pytest.raises(ValueError):
        ExecutionLimits(**{field: value})


def test_worktree_mount_rejects_special_paths_and_host_socket(boundary, tmp_path):
    from lingnexus.tools import DockerExecutor, FileAccessDenied

    dangerous = tmp_path / "bad,readonly"
    dangerous.mkdir()
    with pytest.raises(ValueError):
        DockerExecutor(dangerous, image="fixture")
    (tmp_path / "docker.sock").write_text("fixture, not an actual socket")
    with pytest.raises(FileAccessDenied):
        executor(tmp_path).execute(["true"])
    assert not boundary.calls


def test_worktree_with_hardlink_is_not_mounted(boundary, tmp_path):
    from lingnexus.tools import FileAccessDenied

    work = tmp_path / "work"
    work.mkdir()
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("external fixture")
    os.link(fixture, work / "hardlink")
    with pytest.raises(FileAccessDenied):
        executor(work).execute(["true"])
    assert not boundary.calls


def test_worktree_with_regular_generated_files_can_be_mounted(boundary, tmp_path):
    nested = tmp_path / "generated"
    nested.mkdir()
    (nested / "fixture.txt").write_text("synthetic fixture", encoding="utf-8")
    result = executor(tmp_path).execute(["true"])
    assert result.status == "success"
    assert any(args[1] == "create" for args in boundary.calls)


def test_worktree_directory_link_is_rejected_before_docker(monkeypatch, tmp_path):
    from test_workspace_tools import make_link

    from lingnexus.tools import FileAccessDenied

    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = work / "escape"
    kind = "junction" if os.name == "nt" else "symlink"
    make_link(link, outside, kind)
    try:
        fake = DockerBoundary()
        monkeypatch.setattr(subprocess, "Popen", fake)
        with pytest.raises(FileAccessDenied):
            executor(work).execute(["true"])
        assert not fake.calls
    finally:
        if kind == "junction":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("metadata", [
    {"os": "linux", "id": IMAGE_ID},
    {"os": "linux", "id": IMAGE_ID, "volumes": []},
])
def test_incomplete_image_inspection_is_not_permission_to_create(boundary, tmp_path, metadata):
    boundary.metadata = metadata
    result = executor(tmp_path).execute(["true"])
    assert result.status == "failed"
    assert not any(args[1] == "create" for args in boundary.calls)


@pytest.mark.parametrize("missing", ["Error", "OOMKilled"])
def test_incomplete_exit_state_remains_unknown(boundary, tmp_path, missing):
    del boundary.state[missing]
    result = executor(tmp_path).execute(["true"])
    assert result.status == "unknown"


def test_timeout_cleanup_failure_preserves_uncertainty(boundary, tmp_path):
    boundary.overrides["start"] = {"timeout": True}
    boundary.overrides["kill"] = {"code": 1}
    boundary.overrides["rm"] = {"timeout": True}
    boundary.state = {"Status": "running", "Running": True, "ExitCode": 0}
    result = executor(tmp_path).execute(["sleep", "100"])
    assert result.status == "unknown" and result.timed_out
    assert result.exit_code is None and not result.cleanup_confirmed
    assert result.container_name.startswith("lingnexus-")


def test_real_subprocess_drains_large_output_and_keeps_actual_exit_code():
    from lingnexus.tools._process import run_process

    result = run_process(
        [sys.executable, "-B", "-c",
         "import os; os.write(1, b'x' * 2000000); os.write(2, b'y' * 2000000); raise SystemExit(7)"],
        timeout=10, max_output_bytes=101,
    )
    assert result.returncode == 7
    assert len(result.stdout) + len(result.stderr) == 101
    assert result.truncated and not result.timed_out and result.error is None


def test_real_subprocess_timeout_kills_only_the_controlled_fixture():
    from lingnexus.tools._process import run_process

    result = run_process([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                         timeout=0.15, max_output_bytes=16)
    assert result.timed_out and result.returncode is not None
    assert result.error is None


def test_keyboard_interrupt_still_cleans_container(boundary, tmp_path):
    boundary.overrides["start"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        executor(tmp_path).execute(["sleep", "100"])
    assert any(args[1] == "kill" for args in boundary.calls)
    assert boundary.calls[-1][1:3] == ["rm", "--force"]


@pytest.fixture
def smoke_image():
    from lingnexus.tools._process import run_process

    reference = os.environ.get("LINGNEXUS_DOCKER_SMOKE_IMAGE")
    if not reference:
        pytest.skip("opt-in local Python image required; tests never pull")
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is not installed")
    inspected = run_process(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                            timeout=10, max_output_bytes=1024)
    assert inspected.ok, inspected
    image_id = inspected.stdout.decode().strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
    return image_id


def assert_smoke_container_removed(name):
    from lingnexus.tools._process import run_process

    assert re.fullmatch(r"lingnexus-[0-9a-f]{32}", name)
    checked = run_process(["docker", "container", "ls", "--all", "--filter",
                           f"name=^/{name}$", "--format", "{{.ID}}"],
                          timeout=10, max_output_bytes=1024)
    assert checked.ok and checked.stdout.strip() == b"", checked


def test_optional_docker_smoke_uses_only_temporary_worktree(tmp_path, smoke_image):
    from lingnexus.tools import DockerExecutor, ExecutionLimits

    tmp_path.chmod(0o777)  # Only this generated fixture; allow UID 65534 on native Linux.
    (tmp_path / "fixture.txt").write_text("synthetic")
    runner = DockerExecutor(tmp_path, image=smoke_image, writable=True)
    result = runner.execute(["/bin/sh", "-c",
                             ("set -eu; test $(id -u) -ne 0; test ! -S /var/run/docker.sock; "
                             "test -r /workspace/fixture.txt && "
                             "! touch /rootfs-write-test && "
                             "printf smoke > /workspace/result.txt; exit 23")])
    assert result.status == "failed" and result.exit_code == 23, result
    assert result.cleanup_confirmed
    assert (tmp_path / "result.txt").read_text() == "smoke"
    assert_smoke_container_removed(result.container_name)
    limited = DockerExecutor(tmp_path, image=smoke_image,
                             limits=ExecutionLimits(timeout_seconds=2))
    timeout = limited.execute(["python", "-u", "-c",
                               "import time; print('timeout-ready'); time.sleep(60)"])
    assert timeout.status == "timeout" and timeout.cleanup_confirmed, timeout
    assert timeout.exit_code == 137 and "timeout-ready" in timeout.stdout, timeout
    assert_smoke_container_removed(timeout.container_name)
    print("DOCKER_EXIT_TIMEOUT_SMOKE " + json.dumps({
        "image": smoke_image, "exit_code": result.exit_code,
        "timeout_status": timeout.status, "timeout_exit_code": timeout.exit_code,
        "cleanup_confirmed": timeout.cleanup_confirmed,
    }))


def test_optional_docker_live_isolation_and_cgroups(tmp_path, smoke_image, monkeypatch):
    from lingnexus.tools import DockerExecutor, ExecutionLimits
    from lingnexus.tools._process import run_process

    work = tmp_path / "generated-worktree"
    work.mkdir(mode=0o777)
    work.chmod(0o777)
    monkeypatch.setenv("LINGNEXUS_SMOKE_HOST_ONLY", "synthetic-not-a-credential")
    # No external files or credentials. This script inspects only its own container.
    probe = r'''
import errno, json, os, socket, time
from pathlib import Path
work = Path('/workspace')
assert os.getuid() == os.getgid() == 65534
assert 'LINGNEXUS_SMOKE_HOST_ONLY' not in os.environ
assert not Path('/var/run/docker.sock').exists()
assert not Path('/run/podman/podman.sock').exists()
try:
    Path('/tmp/lingnexus-rootfs-probe').write_text('must fail')
except OSError as exc:
    assert exc.errno == errno.EROFS, exc
else:
    raise AssertionError('root filesystem is writable')
interfaces = [name for _, name in socket.if_nameindex()]
assert interfaces == ['lo'], interfaces
with socket.socket() as connection:
    connection.settimeout(1)
    try:
        connection.connect(('192.0.2.1', 9))  # TEST-NET-1, only after confirming loopback-only.
    except OSError as exc:
        assert exc.errno == errno.ENETUNREACH, exc
        network_errno = exc.errno
    else:
        raise AssertionError('unexpected external connection')
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert status['Seccomp'].strip() == '2'
cgroup = Path('/sys/fs/cgroup')
evidence = {
    'hostname': socket.gethostname(), 'uid': os.getuid(), 'gid': os.getgid(),
    'interfaces': interfaces, 'network_errno': network_errno,
    'cap_eff': status['CapEff'].strip(), 'no_new_privs': status['NoNewPrivs'].strip(),
    'seccomp': status['Seccomp'].strip(),
    'memory_max': (cgroup / 'memory.max').read_text().strip(),
    'memory_swap_max': (cgroup / 'memory.swap.max').read_text().strip(),
    'cpu_max': (cgroup / 'cpu.max').read_text().strip(),
    'pids_max': (cgroup / 'pids.max').read_text().strip(),
}
(work / 'artifact.txt').write_text('generated by isolated Python', encoding='utf-8')
(work / 'report.tmp').write_text(json.dumps(evidence), encoding='utf-8')
os.replace(work / 'report.tmp', work / 'report.json')
while not (work / 'release').exists():
    time.sleep(0.05)
print('live-isolation-ok', flush=True)
'''
    (work / "probe.py").write_text(probe, encoding="utf-8")
    limits = ExecutionLimits(timeout_seconds=30, memory_bytes=134_217_728, cpus=0.5, pids_limit=32)
    runner = DockerExecutor(work, image=smoke_image, writable=True, limits=limits)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.execute, ["python", "-B", "/workspace/probe.py"])
        try:
            deadline = time.monotonic() + 20
            while not (work / "report.json").exists() and not future.done():
                assert time.monotonic() < deadline, "container probe did not become ready"
                time.sleep(0.05)
            assert (work / "report.json").exists(), future.result(timeout=40)
            evidence = json.loads((work / "report.json").read_text(encoding="utf-8"))
            assert re.fullmatch(r"[0-9a-f]{12}", evidence["hostname"])
            fmt = ('{"image":{{json .Image}},"name":{{json .Name}},'
                   '"user":{{json .Config.User}},"running":{{json .State.Running}},'
                   '"host":{{json .HostConfig}},"mounts":{{json .Mounts}}}')
            inspected = run_process(["docker", "inspect", "--format", fmt, evidence["hostname"]],
                                    timeout=10, max_output_bytes=16_384)
            assert inspected.ok and not inspected.truncated, inspected
            live = json.loads(inspected.stdout)
            assert live["running"] is True and live["image"] == smoke_image
            assert live["name"].startswith("/lingnexus-") and live["user"] == "65534:65534"
            host = live["host"]
            assert host["NetworkMode"] == "none"
            assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
            assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
            assert "no-new-privileges=true" in host["SecurityOpt"]
            assert host["Memory"] == host["MemorySwap"] == limits.memory_bytes
            assert host["NanoCpus"] == 500_000_000 and host["PidsLimit"] == 32
            assert host["LogConfig"]["Type"] == "none"
            assert len(live["mounts"]) == 1
            mount = live["mounts"][0]
            assert mount["Type"] == "bind" and mount["Destination"] == "/workspace"
            assert mount["RW"] is True and "docker.sock" not in mount["Source"]
            assert evidence["memory_max"] == str(limits.memory_bytes)
            assert evidence["memory_swap_max"] == "0" and evidence["pids_max"] == "32"
            quota, period = map(int, evidence["cpu_max"].split())
            assert quota / period == 0.5
            assert (work / "artifact.txt").read_text() == "generated by isolated Python"
        finally:
            (work / "release").write_text("finish", encoding="utf-8")
            result = future.result(timeout=40)
    assert result.status == "success" and result.exit_code == 0, result
    assert result.cleanup_confirmed and result.stdout.strip() == "live-isolation-ok", result
    assert_smoke_container_removed(result.container_name)
    print("DOCKER_LIVE_ISOLATION " + json.dumps({
        "image": smoke_image, **evidence, "network_mode": host["NetworkMode"],
        "read_only_root": host["ReadonlyRootfs"], "mount_count": len(live["mounts"]),
        "cleanup_confirmed": result.cleanup_confirmed,
    }, sort_keys=True))


def test_optional_docker_readonly_mount_and_bounded_output(tmp_path, smoke_image):
    from lingnexus.tools import DockerExecutor, ExecutionLimits

    tmp_path.chmod(0o755)
    (tmp_path / "fixture.txt").write_text("synthetic", encoding="utf-8")
    probe = r'''
import errno, sys
from pathlib import Path
assert Path('/workspace/fixture.txt').read_text() == 'synthetic'
try:
    Path('/workspace/forbidden.txt').write_text('must fail')
except OSError as exc:
    assert exc.errno == errno.EROFS, exc
else:
    raise AssertionError('default worktree binding is writable')
sys.stdout.write('x' * 100000)
sys.stdout.flush()
sys.stderr.write('y' * 100000)
'''
    runner = DockerExecutor(tmp_path, image=smoke_image, limits=ExecutionLimits(max_output_bytes=257))
    result = runner.execute(["python", "-B", "-c", probe])
    assert result.status == "success" and result.exit_code == 0, result
    assert len(result.stdout.encode()) + len(result.stderr.encode()) == 257
    assert result.output_truncated and result.cleanup_confirmed
    assert not (tmp_path / "forbidden.txt").exists()
    assert_smoke_container_removed(result.container_name)
    print("DOCKER_BOUNDED_OUTPUT " + json.dumps({
        "captured_bytes": 257, "emitted_bytes": 200000, "truncated": result.output_truncated,
        "readonly_bind_verified": True, "cleanup_confirmed": result.cleanup_confirmed,
    }))
