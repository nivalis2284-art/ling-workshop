"""Deterministic, bounded workspace scanning shared by evidence and snapshots.

One explicit policy: which names are pruned, which limits abort the scan, and
which bytes produced a version. Links, hard links and nested mounts are refused
before any content is read, so no scan silently covers the wrong tree. The
returned ``version`` folds in :data:`SCAN_POLICY_VERSION`; changing exclusion or
limit policy must bump that constant so older evidence is invalidated rather
than silently reinterpreted. Content is always hashed from the bytes read; no
mtime/size cache is trusted, because a same-size edit with a restored timestamp
must still change the fingerprint.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from lingnexus.tools._paths import (
    FileAccessDenied,
    directory,
    open_file,
    regular_file,
    reject_link,
)

SCAN_POLICY_VERSION = "workspace-scan/1"

EXCLUDED = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp",
        ".home",
        ".deps",
        ".artifacts",
    }
)
EXCLUDED_PREFIXES = (".env",)

MAX_FILES = 10_000
MAX_TOTAL = 64 * 1_048_576
MAX_FILE = 8 * 1_048_576
MAX_ENTRIES = 50_000

CHUNK = 256 * 1024
_READ_ATTEMPTS = 3


def policy() -> dict:
    return {"version": SCAN_POLICY_VERSION, "excluded": sorted(EXCLUDED),
            "excluded_prefixes": list(EXCLUDED_PREFIXES), "max_files": MAX_FILES,
            "max_bytes": MAX_TOTAL, "max_file_bytes": MAX_FILE, "max_entries": MAX_ENTRIES}


class ScanError(ValueError):
    """A bounded scan refused to continue. ``code`` is stable for callers."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def excluded(path) -> bool:
    """True when any path component is scratch/dependency noise or an env file.

    Comparison uses ``casefold`` on every component, so the rule holds on
    case-insensitive filesystems instead of depending on their folding.
    """
    for part in Path(path).parts:
        folded = part.casefold()
        if folded in EXCLUDED or folded.startswith(EXCLUDED_PREFIXES):
            return True
    return False


def scan(root, *, include_content: bool = False) -> dict:
    """Walk ``root`` under the module limits and return a content identity.

    Keys: ``version``, ``hashes`` (relative posix path -> sha256 hex), ``files``
    (relative posix path -> bytes, only when ``include_content`` is true, else
    empty), ``count`` and ``bytes``. Excluded entries are pruned before any
    stat or read; links, hard links and nested mounts raise. Limit failures are
    :class:`ScanError` with code ``file_count``, ``total_bytes``, ``file_bytes``
    or ``entry_count``; a file that keeps changing mid-read raises code
    ``file_changed`` instead of reporting a torn hash.
    """
    root = directory(root)
    hashes: dict[str, str] = {}
    files: dict[str, bytes] = {}
    count = 0
    total = 0
    entries = 0
    pending = [root]
    while pending:
        folder = directory(pending.pop())
        with os.scandir(folder) as children:
            for entry in children:
                path = Path(entry.path)
                relative = path.relative_to(root)
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ScanError(
                        "entry_count", "Workspace scan exceeded the directory entry limit."
                    )
                if excluded(relative):
                    continue
                info = path.lstat()
                reject_link(info)
                if entry.is_dir(follow_symlinks=False):
                    if path.is_mount():
                        raise FileAccessDenied("Nested mounts cannot be scanned.")
                    pending.append(directory(path))
                    continue
                regular_file(info)
                if info.st_size > MAX_FILE:
                    raise ScanError("file_bytes", "A file exceeds the scan size limit.")
                name = relative.as_posix()
                digest, size, data = _read_stable(path, include_content=include_content)
                if count + 1 > MAX_FILES:
                    raise ScanError("file_count", "Workspace scan exceeded the file limit.")
                if total + size > MAX_TOTAL:
                    raise ScanError("total_bytes", "Workspace scan exceeded the total size limit.")
                hashes[name] = digest
                count += 1
                total += size
                if data is not None:
                    files[name] = data
    return {
        "version": _version(hashes),
        "hashes": hashes,
        "files": files,
        "count": count,
        "bytes": total,
    }


def _version(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    digest.update(SCAN_POLICY_VERSION.encode())
    for name in sorted(hashes):
        digest.update(b"\0" + name.encode() + b"\0" + bytes.fromhex(hashes[name]))
    return SCAN_POLICY_VERSION + ":" + digest.hexdigest()


def _read_stable(path: Path, *, include_content: bool):
    """Hash one file from its bytes, refusing a version that changed mid-read.

    The handle's own ``fstat`` before and after the read must agree; a same-size
    rewrite with a restored mtime still changes the hash because the hash is
    computed from content, never from metadata. If the file keeps changing, no
    single version exists and the scan refuses instead of reporting a torn one.
    """
    for _ in range(_READ_ATTEMPTS):
        buffer = bytearray() if include_content else None
        digest = hashlib.sha256()
        size = 0
        with open_file(path, "read") as stream:
            before = os.fstat(stream.fileno())
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE:
                    raise ScanError("file_bytes", "A file exceeds the scan size limit.")
                digest.update(chunk)
                if buffer is not None:
                    buffer.extend(chunk)
            after = os.fstat(stream.fileno())
        if _unchanged(before, after):
            return digest.hexdigest(), size, bytes(buffer) if buffer is not None else None
    raise ScanError("file_changed", "A file kept changing during the scan.")


def _unchanged(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
        and before.st_ino == after.st_ino
        and before.st_dev == after.st_dev
    )
