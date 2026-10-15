"""Fail-closed local paths and handle-based opens; no authorization decisions."""
import errno
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path, PureWindowsPath


class FileAccessDenied(PermissionError):
    """A path is outside the supplied grants or uses an unsafe alias."""


def local_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw):
        raise FileAccessDenied("Expected a nonempty local file path")
    windows = PureWindowsPath(raw)
    if raw.startswith(("\\", "//")) or (windows.drive and not windows.is_absolute()):
        raise FileAccessDenied("UNC, device and drive-relative paths are not supported")
    # A rooted path without a drive ("\\foo", "/foo") names the current drive's
    # root and is resolved by whoever joins it — the same ambiguity as a
    # drive-relative path. Refuse it wherever the local filesystem would not
    # call the result absolute, so a POSIX "/foo" (genuinely absolute) survives.
    local = Path(raw.replace("\\", "/"))
    if windows.root and not windows.drive and not local.is_absolute():
        raise FileAccessDenied("Root-relative paths are not supported")
    # Apply Windows alias rules on every platform so grants are portable.
    parts = raw.replace("\\", "/").split("/")
    if windows.drive:
        if os.name != "nt":
            raise FileAccessDenied("Foreign drive path")
        parts = parts[1:]
    for part in parts:
        if part == ".." or (part and part != "." and (
            part.endswith((".", " ")) or any(c in part for c in ':*?"<>|')
            or PureWindowsPath(part).is_reserved()
        )):
            raise FileAccessDenied("Traversal or ambiguous filename")
    return local


def is_link(info: os.stat_result) -> bool:
    """True for any alias that redirects elsewhere.

    ``S_ISLNK`` alone misses Windows junctions and mount points: those report as
    ordinary directories (``S_ISDIR``) and only betray themselves through
    ``FILE_ATTRIBUTE_REPARSE_POINT``. Both halves are required, so this is the one
    predicate every containment check should share.
    """
    return bool(
        stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
    )


def reject_link(info: os.stat_result) -> None:
    if is_link(info):
        raise FileAccessDenied("Symlinks, junctions and reparse points are not allowed")


def directory(value: str | os.PathLike[str]) -> Path:
    path = local_path(value)
    if not path.is_absolute():
        raise ValueError("An absolute existing directory is required")
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        reject_link(info)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(str(component))
    return path


def regular_file(info: os.stat_result) -> None:
    reject_link(info)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FileAccessDenied("Only regular files with one link are allowed")


def check_tree(root: Path, *, max_entries: int = 100_000) -> None:
    """Preflight a Docker bind tree. Caller must prevent concurrent host changes."""
    directory(root)
    pending = [root]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > max_entries:
                    raise FileAccessDenied("Worktree exceeds the inspection limit")
                # Windows DirEntry.stat reports st_nlink=0 even for ordinary files.
                # A fresh lstat is required to distinguish regular files from hardlinks.
                info = Path(entry.path).lstat()
                reject_link(info)
                if entry.name.lower() in {"docker.sock", "podman.sock", "docker_engine"}:
                    raise FileAccessDenied("Container engine endpoints cannot be mounted")
                if stat.S_ISDIR(info.st_mode):
                    if os.path.ismount(entry.path):
                        raise FileAccessDenied("Nested mount points are not allowed")
                    pending.append(Path(entry.path))
                else:
                    regular_file(info)


@contextmanager
def open_file(path: Path, operation: str):
    """Open without following links; validate before any read or truncation.

    Windows holds every ancestor without FILE_SHARE_DELETE while using the file.
    POSIX walks directory descriptors with O_NOFOLLOW. Neither permits a linked
    leaf. These checks do not replace an OS sandbox for arbitrary code execution.
    """
    with ExitStack() as stack:
        if os.name == "nt":
            fd = _windows_open(path, operation, stack)
        else:
            fd = _posix_open(path, operation, stack)
        regular_file(os.fstat(fd))
        mode = "rb" if operation == "read" else "wb"
        # closefd=False: ExitStack owns the OS descriptor, including validation failures.
        with os.fdopen(fd, mode, closefd=False) as stream:
            yield stream


@contextmanager
def open_parent(path: Path):
    """Hold each verified ancestor while replacing a leaf in its directory."""
    with ExitStack() as stack:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            for parent in reversed(path.parents):
                # Sharing read/write but not delete pins the validated ancestry.
                handle = create(str(parent), 0, 0x1 | 0x2, None, 3,
                                0x00200000 | 0x02000000, None)
                if handle == wintypes.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                stack.callback(close, handle)
                info = parent.lstat()
                reject_link(info)
                if not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError(str(parent))
            yield None
            return

        directory_flag = getattr(os, "O_DIRECTORY", None)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if directory_flag is None or nofollow is None:
            raise FileAccessDenied("This platform lacks secure no-follow directory opens")
        descriptor = os.open(path.anchor, os.O_RDONLY | directory_flag)
        stack.callback(os.close, descriptor)
        for part in path.parts[1:-1]:
            descriptor = os.open(part, os.O_RDONLY | directory_flag | nofollow,
                                 dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        yield descriptor


@contextmanager
def open_child(parent_fd: int, path: Path, operation: str):
    """Open a leaf relative to a held parent directory without following links."""
    with ExitStack() as stack:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        nonblock = getattr(os, "O_NONBLOCK", None)
        if nofollow is None or nonblock is None:
            raise FileAccessDenied("This platform lacks secure no-follow file opens")
        flags = os.O_RDONLY if operation == "read" else os.O_WRONLY
        if operation == "create":
            flags |= os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path.name, flags | nofollow | nonblock, 0o600,
                         dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise FileAccessDenied("Linked or non-directory path component") from exc
            raise
        stack.callback(os.close, fd)
        regular_file(os.fstat(fd))
        mode = "rb" if operation == "read" else "wb"
        with os.fdopen(fd, mode, closefd=False) as stream:
            yield stream


def _posix_open(path: Path, operation: str, stack: ExitStack) -> int:
    # These constants do not exist in Windows' os type stubs or runtime.
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if directory_flag is None or nofollow is None or nonblock is None:
        raise FileAccessDenied("This platform lacks secure no-follow file opens")
    parent = os.open(path.anchor, os.O_RDONLY | directory_flag)
    stack.callback(os.close, parent)
    try:
        for part in path.parts[1:-1]:
            parent = os.open(part, os.O_RDONLY | directory_flag | nofollow, dir_fd=parent)
            stack.callback(os.close, parent)
        flags = os.O_RDONLY if operation == "read" else os.O_WRONLY
        if operation == "create":
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path.name, flags | nofollow | nonblock, 0o600, dir_fd=parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise FileAccessDenied("Linked or non-directory path component") from exc
        raise
    stack.callback(os.close, fd)
    return fd


def _windows_open(path: Path, operation: str, stack: ExitStack) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    def handle_for(target: Path, access: int, disposition: int):
        # OPEN_REPARSE_POINT | BACKUP_SEMANTICS; READ/WRITE sharing, never DELETE.
        handle = create(str(target), access, 0x1 | 0x2, None, disposition,
                        0x00200000 | 0x02000000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    for parent in reversed(path.parents):
        handle = handle_for(parent, 0, 3)  # OPEN_EXISTING, metadata only
        stack.callback(close, handle)
        info = parent.lstat()
        reject_link(info)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(str(parent))
    access = 0x80000000 if operation == "read" else 0x40000000
    handle = handle_for(path, access, 1 if operation == "create" else 3)
    try:
        # Inspect the held leaf before converting its handle or touching contents.
        regular_file(path.lstat())
        flags = os.O_RDONLY if operation == "read" else os.O_WRONLY
        fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    stack.callback(os.close, fd)
    return fd
