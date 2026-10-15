"""Explicit directory grants for file tools, supplied by a trusted caller."""
import hashlib
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ._paths import (
    FileAccessDenied,
    directory,
    local_path,
    open_child,
    open_file,
    open_parent,
    regular_file,
)


class FileLimitExceeded(ValueError):
    """The requested read or write exceeds the configured byte limit."""


@dataclass(frozen=True)
class FilePermissions:
    readable_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()

    def __post_init__(self):
        for field in ("readable_roots", "writable_roots"):
            object.__setattr__(self, field, tuple(directory(root) for root in getattr(self, field)))


class WorkspaceTools:
    """Relative paths use workspace; absolute paths still require explicit grants.

    create_* is exclusive, write_* requires an existing file, neither creates
    parents. Write grants never imply read grants. Configuration is trusted;
    authorization, revocation and serialization belong to the parent application.
    """

    def __init__(self, workspace: str | os.PathLike[str], permissions: FilePermissions,
                 *, max_read_bytes: int = 1_048_576, max_write_bytes: int = 1_048_576):
        self.workspace = directory(workspace)
        self.permissions = permissions
        for limit in (max_read_bytes, max_write_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("File limits must be positive integer byte counts")
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes

    def _path(self, path: str | os.PathLike[str], *, write: bool = False) -> Path:
        candidate = local_path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        roots = self.permissions.writable_roots if write else self.permissions.readable_roots
        if not any(candidate.is_relative_to(root) for root in roots):
            raise FileAccessDenied("Path is outside the supplied directory grants")
        return candidate

    def read_bytes(self, path: str | os.PathLike[str]) -> bytes:
        with open_file(self._path(path), "read") as stream:
            data = stream.read(self.max_read_bytes + 1)
        if len(data) > self.max_read_bytes:
            raise FileLimitExceeded("File exceeds the read limit")
        return data

    def read_text(self, path: str | os.PathLike[str]) -> str:
        return self.read_bytes(path).decode("utf-8")

    def _write(self, path: str | os.PathLike[str], data: bytes, operation: str) -> None:
        candidate = self._path(path, write=True)
        if not isinstance(data, bytes):
            raise TypeError("Expected bytes")
        if len(data) > self.max_write_bytes:
            raise FileLimitExceeded("Data exceeds the write limit")
        with open_parent(candidate) as parent_fd:
            if operation == "create":
                created_identity = None
                try:
                    with _open_leaf(candidate, operation, parent_fd) as stream:
                        created_identity = _object_identity(os.fstat(stream.fileno()))
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    _remove_created_file(candidate, created_identity, parent_fd)
                    raise
                return

            # Preserve the old write-access requirement before replacing the
            # inode; an application write grant alone cannot bypass OS mode bits.
            with _open_leaf(candidate, "write", parent_fd):
                pass
            original_stat, original_digest = _read_version(candidate, parent_fd)
            mode = stat.S_IMODE(original_stat.st_mode)
            descriptor, temporary, temporary_name = _create_temporary(candidate, parent_fd)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    if hasattr(os, "fchmod"):
                        os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                current_stat, current_digest = _read_version(candidate, parent_fd)
                changed_identity = _file_identity(current_stat) != _file_identity(original_stat)
                if changed_identity or current_digest != original_digest:
                    raise OSError("File changed during write; replacement was not applied")
                if not hasattr(os, "fchmod"):
                    os.chmod(temporary, mode)
                _replace_temporary(candidate, temporary, temporary_name, parent_fd)
            finally:
                _remove_temporary(temporary, temporary_name, parent_fd)

    def create_bytes(self, path: str | os.PathLike[str], data: bytes) -> None:
        self._write(path, data, "create")

    def write_bytes(self, path: str | os.PathLike[str], data: bytes) -> None:
        self._write(path, data, "write")

    def create_text(self, path: str | os.PathLike[str], text: str) -> None:
        self.create_bytes(path, text.encode("utf-8"))

    def write_text(self, path: str | os.PathLike[str], text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))


def _open_leaf(path: Path, operation: str, parent_fd: int | None):
    if parent_fd is not None:
        return open_child(parent_fd, path, operation)
    return open_file(path, operation)


def _read_version(path: Path, parent_fd: int | None):
    """Read one authorized target version without following aliases."""
    with _open_leaf(path, "read", parent_fd) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if _file_identity(before) != _file_identity(after):
        raise OSError("File changed while its version was being checked")
    return after, digest.digest()


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _object_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _remove_created_file(
    path: Path,
    identity: tuple[int, int] | None,
    parent_fd: int | None,
) -> None:
    """Remove only the still-identical file created by a failed exclusive write."""
    if identity is None:
        return
    try:
        info = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else path.lstat()
        )
        regular_file(info)
        if _object_identity(info) == identity:
            os.unlink(path.name, dir_fd=parent_fd) if parent_fd is not None else path.unlink()
    except (OSError, FileAccessDenied):
        # A vanished, linked, or replaced target is not ours to clean up.
        return


def _create_temporary(candidate: Path, parent_fd: int | None):
    if parent_fd is None:
        descriptor, name = tempfile.mkstemp(prefix=".ling-write-", dir=candidate.parent)
        return descriptor, Path(name), Path(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(16):
        name = ".ling-write-" + secrets.token_hex(12)
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
            return descriptor, candidate.parent / name, name
        except FileExistsError:
            continue
    raise FileExistsError("Unable to allocate a unique temporary file")


def _replace_temporary(
    candidate: Path,
    temporary: Path,
    temporary_name: Path | str,
    parent_fd: int | None,
) -> None:
    if parent_fd is None:
        os.replace(temporary, candidate)
    else:
        os.replace(temporary_name, candidate.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _remove_temporary(
    temporary: Path,
    temporary_name: Path | str,
    parent_fd: int | None,
) -> None:
    try:
        if parent_fd is None:
            temporary.unlink(missing_ok=True)
        else:
            os.unlink(temporary_name, dir_fd=parent_fd)
    except OSError:
        pass
