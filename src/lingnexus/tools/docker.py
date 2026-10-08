"""Docker CLI execution for an already-authorized, exclusively owned worktree."""
import json
import math
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from ._paths import check_tree, directory
from ._process import ProcessResult, run_process


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float = 30.0
    control_timeout_seconds: float = 5.0
    memory_bytes: int = 268_435_456
    cpus: float = 1.0
    pids_limit: int = 64
    max_output_bytes: int = 65_536

    def __post_init__(self):
        for field in ("timeout_seconds", "control_timeout_seconds", "cpus"):
            value = getattr(self, field)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field} must be finite and positive")
        for field in ("memory_bytes", "pids_limit", "max_output_bytes"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.memory_bytes < 6 * 1024 * 1024:
            raise ValueError("Docker requires at least 6 MiB of memory")


@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["success", "failed", "timeout", "unknown"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    timed_out: bool = False
    container_name: str | None = None
    cleanup_confirmed: bool = False
    error: str | None = None


class DockerExecutor:
    """Never pulls an image or grants access. The parent owns authorization.

    Only worktree is bound, read-only unless writable=True. The image must exist
    locally, target Linux and declare no volumes. The parent's Docker context
    must point at the trusted local Linux engine. No host shell is used. Network
    mode is a trusted constructor input, never a model command/tool setting.
    """

    def __init__(self, worktree: str | os.PathLike[str], *, image: str,
                 writable: bool = False, limits: ExecutionLimits | None = None,
                 docker_binary: str = "docker",
                 network_mode: Literal["none", "bridge"] = "none"):
        self.worktree = directory(worktree)
        if any(c in str(self.worktree) for c in ',"\r\n'):
            raise ValueError("Worktree cannot contain Docker mount delimiters")
        if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]*", image):
            raise ValueError("Expected a Docker image reference")
        if type(writable) is not bool:
            raise ValueError("writable must be an explicit boolean")
        if type(network_mode) is not str or network_mode not in ("none", "bridge"):
            raise ValueError("Only none or bridge networking is allowed")
        if not docker_binary or docker_binary.lower().endswith((".bat", ".cmd")):
            raise ValueError("Docker must be a native executable, not a host shell script")
        self.image = image
        self.writable = writable
        self.limits = limits or ExecutionLimits()
        self.docker_binary = docker_binary
        self.network_mode = network_mode

    def _control(self, args: list[str]) -> ProcessResult:
        return run_process([self.docker_binary, *args],
                           timeout=self.limits.control_timeout_seconds, max_output_bytes=16_384)

    def _create_args(self, name: str, image_id: str, argv: list[str]) -> list[str]:
        mount = f"type=bind,source={self.worktree},target=/workspace,bind-recursive=disabled"
        if not self.writable:
            mount += ",readonly"
        return [
            "create", "--name", name, "--pull", "never", "--user", "65534:65534",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--network", self.network_mode, "--memory", str(self.limits.memory_bytes),
            "--memory-swap", str(self.limits.memory_bytes), "--cpus", str(float(self.limits.cpus)),
            "--pids-limit", str(self.limits.pids_limit), "--log-driver", "none",
            "--no-healthcheck", "--restart", "no", "--workdir", "/workspace",
            "--mount", mount, "--entrypoint", argv[0], image_id, *argv[1:],
        ]

    def execute(self, argv: Sequence[str]) -> ExecutionResult:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("Command must be a nonempty argument list")
        if any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValueError("Command arguments must be strings without NUL")
        command = list(argv)
        if not command[0] or command[0].startswith("-"):
            raise ValueError("Expected an executable, not a Docker option")
        check_tree(self.worktree)
        metadata = self._control([
            "image", "inspect", "--format",
            '{"os":{{json .Os}},"id":{{json .Id}},"volumes":{{json (index .Config "Volumes")}}}',
            self.image,
        ])
        image = _json_result(metadata)
        if (not image or image.get("os") != "linux" or "volumes" not in image
                or image["volumes"] not in (None, {})
                or not isinstance(image.get("id"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["id"])):
            return ExecutionResult("failed", error="local_linux_image_required")

        name = "lingnexus-" + uuid4().hex
        deadline = time.monotonic() + self.limits.timeout_seconds
        started = ProcessResult()
        status: Literal["success", "failed", "timeout", "unknown"] = "unknown"
        exit_code = None
        error = None
        timed_out = False
        cleanup = ProcessResult()
        inspected = False
        try:
            created = run_process([self.docker_binary, *self._create_args(name, image["id"], command)],
                                  timeout=min(self.limits.control_timeout_seconds,
                                              self.limits.timeout_seconds), max_output_bytes=16_384)
            timed_out = created.timed_out
            if created.ok:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                else:
                    started = run_process([self.docker_binary, "start", "--attach", name],
                                          timeout=remaining,
                                          max_output_bytes=self.limits.max_output_bytes)
                    timed_out = started.timed_out
            else:
                error = "container_create_failed"
            if timed_out or not started.ok:
                self._control(["kill", name])
            state = _json_result(self._control(["inspect", "--format", "{{json .State}}", name]))
            inspected = True
            if (state and state.get("Status") == "exited" and state.get("Running") is False
                    and type(state.get("ExitCode")) is int and 0 <= state["ExitCode"] <= 255):
                exit_code = state["ExitCode"]
                if not isinstance(state.get("Error"), str) or type(state.get("OOMKilled")) is not bool:
                    error = "container_exit_unconfirmed"
                elif (created.ok and not started.timed_out and started.error is None
                      and started.returncode in (0, exit_code)):
                    # Attached Docker CLI may relay the container's nonzero exit.
                    # The inspected state remains authoritative, not the CLI alone.
                    status = "failed" if exit_code or state.get("Error") or state.get("OOMKilled") \
                        else "success"
                    if state["Error"] or state["OOMKilled"]:
                        error = "container_runtime_error"
                else:
                    error = error or "container_start_unconfirmed"
            else:
                error = error or "container_exit_unconfirmed"
            if not created.ok and not timed_out:
                status = "failed"
            if timed_out:
                status, error = "timeout", "execution_timed_out"
        finally:
            if not inspected:
                self._control(["kill", name])
            cleanup = self._control(["rm", "--force", name])
        if not cleanup.ok:
            status, error = "unknown", "container_cleanup_unconfirmed"
        return ExecutionResult(
            status, exit_code, started.stdout.decode("utf-8", errors="replace"),
            started.stderr.decode("utf-8", errors="replace"), started.truncated,
            timed_out, name, cleanup.ok, error,
        )


def _json_result(result: ProcessResult) -> dict | None:
    if not result.ok or result.truncated:
        return None
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None
