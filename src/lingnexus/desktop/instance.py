"""An OS-owned lock and a disposable owner session, scoped to one data directory."""

import errno
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class Session:
    pid: int
    port: int
    token: str = field(repr=False)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        # Fragments never travel in HTTP requests or server access logs.
        return f"{self.origin}/#token={self.token}"


class InstanceLock:
    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir).expanduser().resolve() / ".desktop"
        self.lock_path = self.directory / "instance.lock"
        self.state_path = self.directory / "owner-session.json"
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._file is not None:
            return True
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = self.lock_path.open("a+b")
        try:
            if file.seek(0, os.SEEK_END) == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            file.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self._file = file
        return True

    def release(self) -> None:
        if self._file is None:
            return
        file, self._file = self._file, None
        try:
            file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
        # Never unlink the lock: contenders must always lock the same inode/file.

    def read_session(self) -> Session | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1:
                return None
            pid, port, token = value["pid"], value["port"], value["token"]
            if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
                return None
            if type(port) is not int or not 0 < port < 65536:
                return None
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
                return None
            return Session(pid, port, token)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write_session(self, session: Session) -> None:
        if self._file is None:
            raise RuntimeError("Writing a desktop session requires the instance lock.")
        temporary = self.directory / f"owner-session-{os.getpid()}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "version": 1,
                        "pid": session.pid,
                        "port": session.port,
                        "token": session.token,
                    },
                    file,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear_session(self, session: Session) -> None:
        if self._file is None:
            raise RuntimeError("Clearing a desktop session requires the instance lock.")
        if self.read_session() == session:
            self.state_path.unlink(missing_ok=True)


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
