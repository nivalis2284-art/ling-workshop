"""Execution port: a default restricted-local tier and an optional container tier.

Two interchangeable implementations sit behind one small interface so task
execution never hard-codes an engine:

* ``RestrictedLocalSandbox`` — the default tier. Argument-list execution with no
  shell, working-copy scoping, time and output bounds, and (where the host can
  enforce them) process-count, memory and CPU-time limits plus process-tree
  cleanup. It is a guard against mistakes and runaway processes, not a
  filesystem or network sandbox; ``describe`` states that plainly.
* ``ContainerSandbox`` — the optional tier. A thin wrapper over the existing
  ``DockerExecutor`` for hosts that already run a container engine.

``probe_engine`` reports whether a usable engine is present; it never switches
tiers by itself.
"""

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from ._jobobject import ResourceLimits
from ._jobobject import available as job_limits_available
from ._paths import directory
from ._process import run_process
from .docker import DockerExecutor, ExecutionLimits, ExecutionResult

SandboxLevel = Literal["local", "container"]
DEFAULT_IMAGE = "python:3.12-slim"
IMAGE_ENV = "LINGNEXUS_SANDBOX_IMAGE"

_ENGINES = ("docker", "podman")
_LOCAL_TMP = ".tmp"
_LOCAL_HOME = ".home"
_LOCAL_DEPS = ".deps/python"
_CONTAINER_ENV = (
    "TMPDIR=/workspace/.tmp", "TMP=/workspace/.tmp", "TEMP=/workspace/.tmp",
    "PYTHONPATH=/workspace/.deps/python", "HOME=/workspace/.home",
)

_LOCAL_PROTECTIONS = (
    "文件工具只能在任务工作副本内读写（路径授权、拒绝父目录穿越与符号链接）",
    "命令以参数列表直接执行，不经过系统命令行解释器，避免命令拼接注入",
    "不向命令传递宿主的其它环境变量（不泄漏 API 密钥等），临时目录与家目录指向工作副本内部",
    "执行超过时限会被终止",
    "输出超过上限会被截断并停止累积",
)
_LOCAL_RESOURCE_PROTECTIONS = (
    "进程数超过上限时，新进程无法创建",
    "内存超过上限时，后续内存分配失败",
    "CPU 时间超过上限时，整个作业被终止",
    "执行结束或终止后，作业内全部进程（含子进程）被清理",
)
_LOCAL_LIMITATIONS = (
    "命令进程本身没有文件系统隔离：仍可读写工作副本以外的文件",
    "没有网络隔离：命令仍可访问网络",
    "不能阻止模型被恶意指令诱导后主动外传数据",
    "不是虚拟机级边界，不能防内核漏洞或提权",
)
_LOCAL_NO_JOB_LIMITATION = "本机没有可用的作业对象，进程数、内存与 CPU 时间上限无法强制"
_CONTAINER_PROTECTIONS = (
    "文件系统隔离：只挂载任务工作副本，容器根文件系统只读",
    "网络隔离：默认无网络（依赖安装经所有者批准时临时开启）",
    "以非 root 用户（UID/GID 65534）运行，丢弃全部 Linux 能力",
    "内存、CPU、进程数上限由容器运行时强制，执行后确认清理",
    "不拉取镜像、不额外挂载、不注入宿主环境",
)
_CONTAINER_LIMITATIONS = (
    "需要本机已安装并运行容器引擎，且目标镜像已存在本地",
    "不是虚拟机级边界，不能防内核漏洞或提权",
    "依赖安装阶段临时开启网络，该窗口内不能阻止外传数据",
)


@dataclass(frozen=True)
class LocalLimits:
    """Bounds for one local run. Values mirror ExecutionLimits plus a CPU budget."""

    timeout_seconds: float = 30.0
    memory_bytes: int = 268_435_456
    cpu_seconds: float = 20.0
    pids_limit: int = 64
    max_output_bytes: int = 65_536

    def __post_init__(self):
        for field in ("timeout_seconds", "cpu_seconds"):
            value = getattr(self, field)
            if type(value) not in (int, float) or value <= 0:
                raise ValueError(f"{field} must be positive")
        for field in ("memory_bytes", "pids_limit", "max_output_bytes"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer")

    @classmethod
    def from_execution_limits(cls, limits: ExecutionLimits) -> "LocalLimits":
        # CPU budget stays generous relative to wall time so a busy but bounded
        # command is not cut short; it still stops a runaway CPU loop.
        return cls(
            timeout_seconds=limits.timeout_seconds,
            memory_bytes=limits.memory_bytes,
            cpu_seconds=max(limits.timeout_seconds * 4, 30.0),
            pids_limit=limits.pids_limit,
            max_output_bytes=limits.max_output_bytes,
        )


@runtime_checkable
class ExecutionSandbox(Protocol):
    level: SandboxLevel

    def execute(self, argv: Sequence[str]) -> ExecutionResult: ...


def configured_image(environ: dict[str, str] | None = None) -> str:
    """The container image, owner-configurable by environment; never model input."""
    source = os.environ if environ is None else environ
    value = (source.get(IMAGE_ENV) or "").strip()
    return value or DEFAULT_IMAGE


def probe_engine(environ: dict[str, str] | None = None) -> dict:
    """Report a usable container engine; requires a reachable local daemon."""
    environment = os.environ if environ is None else environ
    for name in _ENGINES:
        binary = shutil.which(name, path=environment.get("PATH"))
        if not binary:
            continue
        result = run_process(
            [binary, "version", "--format", "{{.Server.Version}}"],
            timeout=5, max_output_bytes=4096,
        )
        if result.ok:
            return {"available": True, "name": name}
    return {"available": False, "name": None}


def describe(level: SandboxLevel, *, job_limits: bool) -> dict:
    """Honest capability statement for a tier; never overstates isolation."""
    if level == "container":
        return {
            "protections": list(_CONTAINER_PROTECTIONS),
            "limitations": list(_CONTAINER_LIMITATIONS),
        }
    protections = list(_LOCAL_PROTECTIONS)
    limitations = list(_LOCAL_LIMITATIONS)
    if job_limits:
        protections.extend(_LOCAL_RESOURCE_PROTECTIONS)
    else:
        limitations.append(_LOCAL_NO_JOB_LIMITATION)
    return {"protections": protections, "limitations": limitations}


def local_environment(worktree: Path) -> dict[str, str]:
    """A minimal child environment: tools resolve, secrets and host vars do not leak."""
    inherited = os.environ
    values = {
        "PATH": inherited.get("PATH", ""),
        "PATHEXT": inherited.get("PATHEXT", ""),
        "SystemRoot": inherited.get("SystemRoot", ""),
        "windir": inherited.get("windir", ""),
        "COMSPEC": inherited.get("COMSPEC", ""),
        "NUMBER_OF_PROCESSORS": inherited.get("NUMBER_OF_PROCESSORS", ""),
        "TMPDIR": str(worktree / _LOCAL_TMP),
        "TMP": str(worktree / _LOCAL_TMP),
        "TEMP": str(worktree / _LOCAL_TMP),
        "HOME": str(worktree / _LOCAL_HOME),
        "USERPROFILE": str(worktree / _LOCAL_HOME),
        "PYTHONPATH": str(worktree / _LOCAL_DEPS),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return {key: value for key, value in values.items() if value}


def _validated(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("Command must be a nonempty argument list")
    if any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise ValueError("Command arguments must be strings without NUL")
    command = list(argv)
    if not command[0] or command[0].startswith("-"):
        raise ValueError("Expected an executable, not an option")
    return command


class RestrictedLocalSandbox:
    """Default tier. Resource limits are enforced only where the host allows."""

    level: SandboxLevel = "local"

    def __init__(self, worktree, *, writable: bool = False, limits: LocalLimits | None = None):
        self.worktree = directory(worktree)
        if type(writable) is not bool:
            raise ValueError("writable must be an explicit boolean")
        self.writable = writable
        self.limits = limits or LocalLimits()
        self.resource_limits_enforced = job_limits_available()

    def execute(self, argv: Sequence[str]) -> ExecutionResult:
        command = _validated(argv)
        self._prepare_directories()
        job = None
        if self.resource_limits_enforced:
            job = ResourceLimits(
                memory_bytes=self.limits.memory_bytes,
                cpu_seconds=self.limits.cpu_seconds,
                pids_limit=self.limits.pids_limit,
            )
        result = run_process(
            command,
            timeout=self.limits.timeout_seconds,
            max_output_bytes=self.limits.max_output_bytes,
            cwd=str(self.worktree),
            env=local_environment(self.worktree),
            job_limits=job,
            new_session=not self.resource_limits_enforced,
        )
        return self._result(result)

    def _prepare_directories(self) -> None:
        for name in (_LOCAL_TMP, _LOCAL_HOME):
            path = self.worktree / name
            if not path.exists():
                path.mkdir(mode=0o700, exist_ok=True)

    def _result(self, result) -> ExecutionResult:
        if result.error == "process_unavailable":
            return ExecutionResult("failed", error="process_unavailable")
        if result.error == "resource_limits_unavailable":
            return ExecutionResult("unknown", error="resource_limits_unavailable")
        if result.timed_out:
            status, error = "timeout", "execution_timed_out"
        elif result.error:
            status, error = "unknown", result.error
        elif result.returncode == 0:
            status, error = "success", None
        else:
            status, error = "failed", None
        # Cleanup is only "confirmed" when the reaper did not report otherwise;
        # claiming it while a kill was unconfirmed would hide a live process.
        return ExecutionResult(
            status, result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
            result.truncated, result.timed_out, None,
            result.error != "process_cleanup_unconfirmed", error,
        )


class ContainerSandbox:
    """Optional tier. Delegates to DockerExecutor with the fixed container env."""

    level: SandboxLevel = "container"

    def __init__(self, worktree, *, image: str, writable: bool = False,
                 limits: ExecutionLimits | None = None,
                 network_mode: Literal["none", "bridge"] = "none"):
        self._executor = DockerExecutor(
            worktree, image=image, writable=writable, limits=limits, network_mode=network_mode
        )
        self.worktree = self._executor.worktree

    def execute(self, argv: Sequence[str]) -> ExecutionResult:
        command = _validated(argv)
        return self._executor.execute(["env", *_CONTAINER_ENV, *command])


def create_executor(
    level: SandboxLevel,
    worktree,
    *,
    image: str | None = None,
    writable: bool = False,
    limits: ExecutionLimits | None = None,
    local_limits: LocalLimits | None = None,
    network_mode: Literal["none", "bridge"] = "none",
) -> ExecutionSandbox:
    """Build the executor for a tier; the only place a tier is chosen."""
    if level == "container":
        if not image:
            raise ValueError("Container tier requires a configured image")
        return ContainerSandbox(
            worktree, image=image, writable=writable, limits=limits, network_mode=network_mode
        )
    if level != "local":
        raise ValueError("Unknown sandbox tier")
    return RestrictedLocalSandbox(worktree, writable=writable, limits=local_limits)
