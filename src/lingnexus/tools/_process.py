"""Bounded binary subprocess capture. Host commands are always argument lists."""
import os
import signal
import subprocess
from dataclasses import dataclass
from threading import Lock, Thread

from ._jobobject import CREATE_SUSPENDED, JobObject, ResourceLimits, resume_process


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None


def run_process(argv: list[str], *, timeout: float, max_output_bytes: int,
                cwd: str | None = None, env: dict[str, str] | None = None,
                job_limits: ResourceLimits | None = None,
                new_session: bool = False) -> ProcessResult:
    """Run an argument list with no shell.

    cwd/env scope the child; job_limits adds Windows Job Object bounds. When a
    job is requested the child is created suspended, assigned, then resumed so
    that no descendant can escape the limits. Closing the job handle at the end
    kills any process the child left behind. On POSIX, new_session places the
    child in its own process group so a timeout can kill the whole group.
    """
    job = None
    creationflags = 0
    if job_limits is not None:
        try:
            job = JobObject(job_limits)
        except OSError:
            return ProcessResult(error="resource_limits_unavailable")
        creationflags = CREATE_SUSPENDED
    try:
        process = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=cwd, env=env, creationflags=creationflags,
                                   start_new_session=new_session)
    except OSError:
        if job is not None:
            job.close()
        return ProcessResult(error="process_unavailable")
    if job is not None:
        try:
            job.assign(process._handle)
        except OSError:
            process.kill()
            _reap(process)
            job.close()
            return ProcessResult(error="resource_limits_unavailable")
        if not resume_process(process._handle):
            process.kill()
            _reap(process)
            job.close()
            return ProcessResult(error="resource_limits_unavailable")
    buffers = [bytearray(), bytearray()]
    lock = Lock()
    remaining = max_output_bytes
    truncated = False
    read_failed = False

    def drain(stream, buffer):
        nonlocal remaining, truncated, read_failed
        try:
            while chunk := stream.read(8192):
                with lock:
                    count = min(remaining, len(chunk))
                    buffer.extend(chunk[:count])
                    remaining -= count
                    truncated |= count < len(chunk)
        except (OSError, ValueError):
            with lock:
                read_failed = True
        finally:
            stream.close()

    threads = [Thread(target=drain, args=(stream, buffer), daemon=True)
               for stream, buffer in zip((process.stdout, process.stderr), buffers)]
    for thread in threads:
        thread.start()
    timed_out = False
    error = None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            _terminate(process, job, new_session)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            error = "process_cleanup_unconfirmed"
    except BaseException:
        _terminate(process, job, new_session)
        _reap(process)
        raise
    finally:
        # Kill-on-close removes any descendant still holding the pipes, so the
        # drains can finish; never leave the job handle open.
        if job is not None:
            job.close()
        for thread in threads:
            thread.join(timeout=2)
    with lock:
        if read_failed or any(thread.is_alive() for thread in threads):
            error = error or "output_incomplete"
        return ProcessResult(process.returncode, bytes(buffers[0]), bytes(buffers[1]),
                             timed_out, truncated, error)


def _reap(process) -> None:
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate(process, job, new_session: bool) -> None:
    if job is not None:
        job.terminate()
        return
    if new_session and os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    process.kill()
