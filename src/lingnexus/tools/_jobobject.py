"""Windows Job Object resource limits through ctypes; no third-party dependency.

A Job Object is the only mechanism available on this host (no psutil, no
resource module) that can bound a process tree. It supplies four guarantees:

* process-count limit (new processes fail to start beyond the cap),
* memory limit (further commits fail),
* CPU-time limit (the whole job is terminated when exceeded),
* kill-on-close (every remaining process in the tree is killed when the job
  handle closes, so no child outlives a run).

None of these is a filesystem or network boundary. On non-Windows hosts the
module reports itself unavailable and callers must degrade honestly.
"""

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

CREATE_SUSPENDED = 0x00000004

_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_CPU_TICKS_PER_SECOND = 10_000_000


@dataclass(frozen=True)
class ResourceLimits:
    """Per-run resource bounds enforced through a Windows Job Object."""

    memory_bytes: int | None = None
    cpu_seconds: float | None = None
    pids_limit: int | None = None

    def __post_init__(self):
        for field in ("memory_bytes", "pids_limit"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{field} must be a positive integer or None")
        if self.cpu_seconds is not None and (
            type(self.cpu_seconds) not in (int, float) or self.cpu_seconds <= 0
        ):
            raise ValueError("cpu_seconds must be a positive number or None")


def available() -> bool:
    """True only where a real job-object guarantee can be created."""
    return os.name == "nt"


if available():
    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class JobObject:
    """One process-tree scope. Create it, assign the suspended child, then run.

    The handle must be closed after the child is reaped; kill-on-close then
    removes every descendant that would otherwise survive.
    """

    def __init__(self, limits: ResourceLimits):
        if not available():
            raise OSError("Job objects require Windows")
        self._kernel = _kernel()
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimitInformation()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if limits.pids_limit is not None:
            info.BasicLimitInformation.ActiveProcessLimit = limits.pids_limit
            flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        if limits.memory_bytes is not None:
            info.ProcessMemoryLimit = limits.memory_bytes
            info.JobMemoryLimit = limits.memory_bytes
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_JOB_MEMORY
        if limits.cpu_seconds is not None:
            ticks = int(limits.cpu_seconds * _CPU_TICKS_PER_SECOND)
            info.BasicLimitInformation.PerProcessUserTimeLimit = ticks
            info.BasicLimitInformation.PerJobUserTimeLimit = ticks
            flags |= _JOB_OBJECT_LIMIT_PROCESS_TIME | _JOB_OBJECT_LIMIT_JOB_TIME
        info.BasicLimitInformation.LimitFlags = flags
        ok = self._kernel.SetInformationJobObject(
            self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process_handle) -> None:
        if not self._kernel.AssignProcessToJobObject(
            self._handle, wintypes.HANDLE(process_handle)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        self._kernel.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def resume_process(process_handle) -> bool:
    """Resume a CREATE_SUSPENDED process; returns False if the call is absent."""
    if not available():
        return False
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    function = getattr(ntdll, "NtResumeProcess", None)
    if function is None:
        return False
    function.argtypes = [wintypes.HANDLE]
    function.restype = ctypes.c_long
    return function(wintypes.HANDLE(process_handle)) == 0
