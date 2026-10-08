"""Explicit directory grants for file tools, supplied by a trusted caller."""
import os
from dataclasses import dataclass
from pathlib import Path

from ._paths import FileAccessDenied, directory, local_path, open_file


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
        with open_file(candidate, operation) as stream:
            # The descriptor is verified before truncating, including hardlink checks.
            stream.truncate(0)
            stream.write(data)

    def create_bytes(self, path: str | os.PathLike[str], data: bytes) -> None:
        self._write(path, data, "create")

    def write_bytes(self, path: str | os.PathLike[str], data: bytes) -> None:
        self._write(path, data, "write")

    def create_text(self, path: str | os.PathLike[str], text: str) -> None:
        self.create_bytes(path, text.encode("utf-8"))

    def write_text(self, path: str | os.PathLike[str], text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))
