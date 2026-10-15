"""Atomic file delivery tests for explicitly granted workspace paths."""

import os

import pytest

from lingnexus.tools._paths import FileAccessDenied
from lingnexus.tools.workspace import FileLimitExceeded, FilePermissions, WorkspaceTools


def _tools(root, *, readable=True, max_write_bytes=1024):
    roots = (root,) if readable else ()
    return WorkspaceTools(
        root,
        FilePermissions(readable_roots=roots, writable_roots=(root,)),
        max_write_bytes=max_write_bytes,
    )


def test_write_replaces_existing_file_with_complete_content(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"old content")
    workspace = _tools(tmp_path)

    workspace.write_bytes("notes.txt", b"a complete replacement")

    assert target.read_bytes() == b"a complete replacement"
    assert list(tmp_path.glob(".ling-write-*")) == []


def test_write_does_not_require_a_read_grant(tmp_path):
    target = tmp_path / "private.txt"
    target.write_bytes(b"old")
    workspace = _tools(tmp_path, readable=False)

    workspace.write_bytes("private.txt", b"new")

    assert target.read_bytes() == b"new"
    with pytest.raises(FileAccessDenied):
        workspace.read_bytes("private.txt")


def test_create_is_exclusive_and_preserves_existing_file(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_bytes(b"owner data")
    workspace = _tools(tmp_path)

    with pytest.raises(FileExistsError):
        workspace.create_bytes("existing.txt", b"replacement")

    assert target.read_bytes() == b"owner data"


def test_size_limit_failure_preserves_existing_content(tmp_path):
    target = tmp_path / "limited.txt"
    target.write_bytes(b"keep")
    workspace = _tools(tmp_path, max_write_bytes=3)

    with pytest.raises(FileLimitExceeded):
        workspace.write_bytes("limited.txt", b"too large")

    assert target.read_bytes() == b"keep"


def test_write_refuses_symlink_without_touching_target(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    workspace = _tools(tmp_path)

    with pytest.raises(FileAccessDenied):
        workspace.write_bytes("link.txt", b"changed")

    assert outside.read_bytes() == b"outside"
    assert link.is_symlink()


def test_write_refuses_hard_link_without_touching_target(tmp_path):
    original = tmp_path / "original.txt"
    original.write_bytes(b"shared")
    alias = tmp_path / "alias.txt"
    try:
        os.link(original, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable")
    workspace = _tools(tmp_path)

    with pytest.raises(FileAccessDenied):
        workspace.write_bytes("alias.txt", b"changed")

    assert original.read_bytes() == b"shared"
    assert alias.read_bytes() == b"shared"


def test_external_change_before_commit_is_preserved(tmp_path, monkeypatch):
    target = tmp_path / "concurrent.txt"
    target.write_bytes(b"initial")
    workspace = _tools(tmp_path)
    real_fsync = os.fsync
    changed = False

    def fsync_then_external_write(fd):
        nonlocal changed
        real_fsync(fd)
        if not changed:
            changed = True
            target.write_bytes(b"external update")

    monkeypatch.setattr(os, "fsync", fsync_then_external_write)

    with pytest.raises(OSError, match="changed during write"):
        workspace.write_bytes("concurrent.txt", b"agent update")

    assert target.read_bytes() == b"external update"
    assert list(tmp_path.glob(".ling-write-*")) == []


def test_replace_failure_keeps_old_file_and_cleans_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "stable.txt"
    target.write_bytes(b"old")
    workspace = _tools(tmp_path)

    def failed_replace(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated replacement failure"):
        workspace.write_bytes("stable.txt", b"new")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".ling-write-*")) == []


def test_failed_create_removes_only_its_partial_file(tmp_path, monkeypatch):
    workspace = _tools(tmp_path)
    target = tmp_path / "new.txt"
    real_fsync = os.fsync

    def replace_then_fail(fd):
        real_fsync(fd)
        if os.name == "nt":
            raise OSError("simulated create failure")
        target.unlink()
        target.write_bytes(b"external replacement")
        raise OSError("simulated create failure")

    monkeypatch.setattr(os, "fsync", replace_then_fail)
    with pytest.raises(OSError, match="simulated create failure"):
        workspace.create_bytes("new.txt", b"partial")

    if os.name == "nt":
        assert not target.exists()
    else:
        assert target.read_bytes() == b"external replacement"
