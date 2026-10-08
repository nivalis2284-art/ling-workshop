"""All filesystem fixtures are synthetic and live below pytest's tmp_path."""
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def sandbox(tmp_path):
    from lingnexus.tools import FilePermissions, WorkspaceTools

    work = tmp_path / "work"
    work.mkdir()
    readable = work / "readable"
    writable = work / "writable"
    shared = work / "shared"
    for directory in (readable, writable, shared):
        directory.mkdir()
    (readable / "input.txt").write_text("fixture input", encoding="utf-8")
    (writable / "output.txt").write_text("private output", encoding="utf-8")
    permissions = FilePermissions(
        readable_roots=(readable, shared), writable_roots=(writable, shared)
    )
    return WorkspaceTools(work, permissions, max_read_bytes=32, max_write_bytes=32), work


def test_separate_read_write_and_explicit_absolute_paths(sandbox):
    from lingnexus.tools import FileAccessDenied

    files, work = sandbox
    assert files.read_text("readable/input.txt") == "fixture input"
    assert files.read_text(work / "readable/input.txt") == "fixture input"
    with pytest.raises(FileAccessDenied):
        files.write_text("readable/input.txt", "changed")
    with pytest.raises(FileAccessDenied):
        files.read_text("writable/output.txt")
    files.write_text("writable/output.txt", "short")
    assert (work / "writable/output.txt").read_text() == "short"
    files.create_text("shared/new.txt", "hello 灵")
    assert files.read_text("shared/new.txt") == "hello 灵"


@pytest.mark.parametrize("path", [
    "../outside.txt", "shared/../../outside.txt", "shared/../readable/input.txt",
    r"shared\..\..\outside.txt", "readable-sibling/input.txt", "outside.txt",
    r"C:relative.txt", r"\\server\share\file.txt", r"\\?\C:\file.txt",
    "shared/file.txt:secret", "shared/NUL", "shared/CON.txt", "shared/x.",
    "shared/x ", "shared/bad\x00name",
])
def test_ambiguous_and_escaping_paths_are_denied(sandbox, path):
    from lingnexus.tools import FileAccessDenied

    files, work = sandbox
    (work / "readable-sibling").mkdir()
    (work / "readable-sibling/input.txt").write_text("sibling")
    (work.parent / "outside.txt").write_text("outside")
    for operation in (files.read_text, lambda p: files.create_text(p, "bad"),
                      lambda p: files.write_text(p, "bad")):
        with pytest.raises(FileAccessDenied):
            operation(path)
    assert (work.parent / "outside.txt").read_text() == "outside"


def test_absolute_outside_and_sibling_prefix_are_denied(sandbox):
    from lingnexus.tools import FileAccessDenied

    files, work = sandbox
    for path in (work.parent / "outside", work / "shared-extra"):
        path.mkdir()
        target = path / "data.txt"
        target.write_text("untouched")
        with pytest.raises(FileAccessDenied):
            files.read_text(target)
        with pytest.raises(FileAccessDenied):
            files.write_text(target, "bad")
        with pytest.raises(FileAccessDenied):
            files.create_text(path / "new.txt", "bad")
        assert target.read_text() == "untouched"


def test_create_write_read_require_correct_existence_and_file_type(sandbox):
    files, work = sandbox
    with pytest.raises(FileExistsError):
        files.create_text("writable/output.txt", "bad")
    with pytest.raises(FileNotFoundError):
        files.write_text("shared/missing.txt", "bad")
    with pytest.raises(FileNotFoundError):
        files.read_text("shared/missing.txt")
    with pytest.raises(FileNotFoundError):
        files.create_text("shared/missing/child.txt", "bad")
    for operation in (files.read_text, lambda p: files.write_text(p, "bad")):
        with pytest.raises((OSError, PermissionError)):
            operation("shared")
    assert not (work / "shared/missing").exists()


def test_file_limits_are_bytes_and_failure_does_not_truncate(sandbox):
    from lingnexus.tools import FileLimitExceeded

    files, work = sandbox
    large = work / "shared/large.txt"
    large.write_bytes(b"x" * 33)
    with pytest.raises(FileLimitExceeded):
        files.read_text(large)
    with pytest.raises(FileLimitExceeded):
        files.write_text(large, "灵" * 11)
    with pytest.raises(FileLimitExceeded):
        files.create_text("shared/new.txt", "x" * 33)
    assert large.read_bytes() == b"x" * 33
    assert not (work / "shared/new.txt").exists()


def make_link(link: Path, target: Path, kind: str):
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows junction test")
        # PowerShell's native cmdlet: arguments arrive as environment values, never code.
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             ("New-Item -ItemType Junction -Path $env:LING_TEST_LINK "
              "-Target $env:LING_TEST_TARGET -ErrorAction Stop | Out-Null")],
            env={**os.environ, "LING_TEST_LINK": str(link), "LING_TEST_TARGET": str(target)},
            capture_output=True, timeout=15, shell=False, check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("Windows symlink privilege not available")
            raise


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_directory_links_cannot_escape_or_become_allowed_roots(sandbox, kind):
    from lingnexus.tools import FileAccessDenied, FilePermissions

    files, work = sandbox
    outside = work.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("fixture secret")
    link = work / "shared/escape"
    make_link(link, outside, kind)
    try:
        with pytest.raises(FileAccessDenied):
            files.read_text(link / "secret.txt")
        with pytest.raises(FileAccessDenied):
            files.write_text(link / "secret.txt", "bad")
        with pytest.raises(FileAccessDenied):
            files.create_text(link / "new.txt", "bad")
        with pytest.raises(FileAccessDenied):
            FilePermissions(readable_roots=(link,))
        assert not (outside / "new.txt").exists()
        assert (outside / "secret.txt").read_text() == "fixture secret"
    finally:
        if kind == "junction":
            link.rmdir()
        else:
            link.unlink()


def test_file_hardlinks_are_denied(sandbox):
    from lingnexus.tools import FileAccessDenied

    files, work = sandbox
    target = work.parent / "secret.txt"
    target.write_text("fixture secret")
    hardlink = work / "shared/hard.txt"
    os.link(target, hardlink)
    for operation in (files.read_text, lambda p: files.write_text(p, "bad")):
        with pytest.raises(FileAccessDenied):
            operation(hardlink)
    assert target.read_text() == "fixture secret"


def test_file_symlinks_are_denied(sandbox):
    from lingnexus.tools import FileAccessDenied

    files, work = sandbox
    target = work.parent / "secret.txt"
    target.write_text("fixture secret")
    link = work / "shared/link.txt"
    make_link(link, target, "symlink")
    with pytest.raises(FileAccessDenied):
        files.read_text(link)
    with pytest.raises(FileAccessDenied):
        files.write_text(link, "bad")
    assert target.read_text() == "fixture secret"


def test_empty_permissions_deny_everything_and_roots_must_exist(tmp_path):
    from lingnexus.tools import FileAccessDenied, FilePermissions, WorkspaceTools

    files = WorkspaceTools(tmp_path, FilePermissions())
    with pytest.raises(FileAccessDenied):
        files.create_text("new.txt", "no grant")
    with pytest.raises((ValueError, FileNotFoundError)):
        FilePermissions(readable_roots=(tmp_path / "missing",))
    with pytest.raises(ValueError):
        FilePermissions(writable_roots=(Path("relative"),))


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handle locking")
def test_windows_holds_ancestors_and_leaf_against_replacement(sandbox):
    from lingnexus.tools._paths import open_file

    _, work = sandbox
    target = work / "readable/input.txt"
    with open_file(target, "read") as stream:
        with pytest.raises(PermissionError):
            target.parent.rename(work / "moved")
        with pytest.raises(PermissionError):
            target.rename(target.with_suffix(".moved"))
        assert stream.read() == b"fixture input"


def test_replaced_allowed_root_is_rechecked_at_operation_time(sandbox):
    from lingnexus.tools import FileAccessDenied

    files, work = sandbox
    granted = work / "shared"
    granted.rmdir()  # Verified empty fixture directory; no recursive deletion.
    outside = work.parent / "outside"
    outside.mkdir()
    kind = "junction" if os.name == "nt" else "symlink"
    make_link(granted, outside, kind)
    try:
        with pytest.raises(FileAccessDenied):
            files.create_text("shared/escape.txt", "no")
        assert not (outside / "escape.txt").exists()
    finally:
        if kind == "junction":
            granted.rmdir()
        else:
            granted.unlink()


def test_binary_round_trip_and_exact_limits(sandbox):
    files, _ = sandbox
    payload = bytes(range(32))
    files.create_bytes("shared/binary.dat", payload)
    assert files.read_bytes("shared/binary.dat") == payload
    files.write_bytes("shared/binary.dat", b"")
    assert files.read_bytes("shared/binary.dat") == b""
