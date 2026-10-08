"""Bounded binary subprocess capture. Host commands are always argument lists."""
import subprocess
from dataclasses import dataclass
from threading import Lock, Thread


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


def run_process(argv: list[str], *, timeout: float, max_output_bytes: int) -> ProcessResult:
    try:
        process = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return ProcessResult(error="process_unavailable")
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
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            error = "process_cleanup_unconfirmed"
    except BaseException:
        process.kill()
        process.wait(timeout=2)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2)
    with lock:
        if read_failed or any(thread.is_alive() for thread in threads):
            error = error or "output_incomplete"
        return ProcessResult(process.returncode, bytes(buffers[0]), bytes(buffers[1]),
                             timed_out, truncated, error)
