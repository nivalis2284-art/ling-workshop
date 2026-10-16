"""Atomic file delivery tests for explicitly granted workspace paths."""

import asyncio
import hashlib
import os
import time
from pathlib import Path

import pytest

from lingnexus.tools._paths import FileAccessDenied
from lingnexus.tools.workspace import (
    FileLimitExceeded,
    FilePermissions,
    FileVersionConflict,
    WorkspaceTools,
)


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


def test_read_version_precondition_refuses_change_before_write_starts(tmp_path):
    target = tmp_path / "concurrent.txt"
    target.write_bytes(b"read version")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_bytes(b"external update")
    with pytest.raises(FileVersionConflict):
        _tools(tmp_path).write_text("concurrent.txt", "agent update", expected_sha256=expected)
    assert target.read_bytes() == b"external update"
    assert not list(tmp_path.glob(".ling-write-*"))


def test_edit_binds_the_read_version_through_actual_chat_write(tmp_path, monkeypatch):
    from lingnexus.actions.dispatch import ToolDispatcher
    from lingnexus.app import Application
    from lingnexus.runtime.scope import ChatToolScope

    app = Application(tmp_path / "edit.db")
    try:
        agent = app.create_agent("edit test")
        readable = tmp_path / "readable"
        writable = tmp_path / "writable"
        readable.mkdir()
        writable.mkdir()
        target = readable / "file.txt"
        target.write_text("a", encoding="utf-8")
        (writable / "file.txt").write_text("different root", encoding="utf-8")
        app.permissions.configure(agent, readable=[readable], writable=[writable, readable],
                                  expected_revision=0)
        scope = ChatToolScope(app, agent, "edit-test")
        original = scope.write

        def concurrent_write(raw, text, **kwargs):
            assert Path(raw) == target
            target.write_text("c", encoding="utf-8")
            return original(raw, text, **kwargs)

        monkeypatch.setattr(scope, "write", concurrent_write)
        result = asyncio.run(ToolDispatcher().execute("edit_file", {
            "path": "file.txt", "old_text": "a", "new_text": "b",
        }, scope))
        assert result.status == "failed"
        assert result.error_code == "edit_version_conflict"
        assert target.read_text(encoding="utf-8") == "c"
        assert (writable / "file.txt").read_text(encoding="utf-8") == "different root"
    finally:
        app.close()


def test_snapshot_write_never_chmods_a_replaceable_leaf(tmp_path, monkeypatch):
    from lingnexus.tasks.execution import TaskToolScope

    app, runtime, task_id, _project, staged, _review = _task_workspace(tmp_path)
    original = Path.chmod
    leaf = staged / "a.txt"

    def reject_leaf_chmod(path, *args, **kwargs):
        assert path != leaf, "a concurrent command can replace this leaf with a link"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", reject_leaf_chmod)
    try:
        scope = TaskToolScope(app, runtime.store, runtime.snapshots, runtime.store.get(task_id),
                              {"id": "effect", "call_id": "write"}, None, None)
        payload, _ = scope.write("a.txt", "safe replacement", create=False)
        assert payload["status"] == "completed"
        assert leaf.read_text(encoding="utf-8") == "safe replacement"
    finally:
        asyncio.run(runtime.aclose())
        app.close()


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
    if not link.is_symlink():
        # Some Windows configurations neither raise nor create the link.
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
    if not alias.exists():
        # Some Windows configurations neither raise nor create the alias.
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


def _task_workspace(tmp_path):
    from lingnexus.app import Application
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    project = tmp_path / "project"
    project.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (project / name).write_text("old-" + name[0], encoding="utf-8")
    app = Application(tmp_path / "task.db")
    agent = app.create_agent("delivery test")
    profile = app.profiles.save(
        "test profile",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="offline-test",
            base_url="https://example.invalid/v1",
        ),
    )
    app.profiles.select(profile["id"], None)
    app.permissions.configure(agent, readable=[project], writable=[project], expected_revision=0)
    runtime = TaskRuntime(app)
    task_id = asyncio.run(runtime.submit({
        "agent_id": agent,
        "objective": "review fixture changes",
        "request_id": "workspace-delivery-test",
        "workspace": str(project),
    }))
    runtime.store.status(task_id, "needs_review")
    staged = runtime.workspace(task_id)
    for name in ("a.txt", "b.txt", "c.txt"):
        (staged / name).write_text("new-" + name[0], encoding="utf-8")
    review = runtime.changes(task_id)
    return app, runtime, task_id, project, staged, review


def test_partial_apply_records_each_file_and_exposes_original_recovery_material(
    tmp_path, monkeypatch
):
    from lingnexus.common.errors import Conflict

    app, runtime, task_id, project, _staged, review = _task_workspace(tmp_path)
    real_execute = app.actions.execute
    calls = []

    def fail_second(agent_id, request_id, name, arguments):
        calls.append(arguments["path"])
        if arguments["path"].endswith("b.txt"):
            raise OSError("injected second-file failure")
        return real_execute(agent_id, request_id, name, arguments)

    monkeypatch.setattr(app.actions, "execute", fail_second)
    try:
        outcome = asyncio.run(runtime.apply(task_id, review_id=review["review_id"]))
        assert outcome["status"] == "unknown"
        assert [(item["path"], item["status"]) for item in outcome["files"]] == [
            ("a.txt", "applied"),
            ("b.txt", "unknown"),
            ("c.txt", "not_applied"),
        ]
        assert len(outcome["effect_ids"]) == 1
        assert (project / "a.txt").read_text(encoding="utf-8") == "new-a"
        assert (project / "b.txt").read_text(encoding="utf-8") == "old-b"
        assert (project / "c.txt").read_text(encoding="utf-8") == "old-c"
        by_path = {item["path"]: item for item in review["files"]}
        assert by_path["b.txt"]["baseline_artifact"] == "__baseline__/b.txt"
        recovery = runtime.artifact(task_id, by_path["b.txt"]["baseline_artifact"])
        assert recovery == b"old-b"
        stored = runtime.store.get_review(task_id, review["review_id"])
        assert stored["status"] == "unknown"
        assert stored["result"]["files"] == outcome["files"]
        with pytest.raises(Conflict):
            asyncio.run(runtime.apply(task_id, review_id=review["review_id"]))
        assert len(calls) == 2
    finally:
        asyncio.run(runtime.aclose())
        app.close()


def test_unknown_task_command_blocks_review_and_apply(tmp_path):
    from lingnexus.common.errors import Conflict

    app, runtime, task_id, _project, _staged, review = _task_workspace(tmp_path)
    try:
        owner = runtime.sandbox._owner(runtime.get(task_id)["agent_id"], task_id, "task")
        session = runtime.sandbox.sessions.start(
            owner, task_id, ["unknown-fixture"],
            lambda _event, _emit: {"status": "unknown", "error": "cleanup_unconfirmed"},
        )
        for _ in range(100):
            view = runtime.sandbox.sessions.poll(session["session_id"], owner)
            if view["status"] == "unknown":
                break
            time.sleep(0.01)
        assert view["status"] == "unknown"
        with pytest.raises(Conflict, match="unknown cleanup status"):
            runtime.changes(task_id)
        with pytest.raises(Conflict, match="unknown cleanup status"):
            asyncio.run(runtime.apply(task_id, review_id=review["review_id"]))
        assert (_project / "a.txt").read_text(encoding="utf-8") == "old-a"
    finally:
        asyncio.run(runtime.aclose())
        app.close()


@pytest.mark.parametrize("changed_area", ["source", "staged"])
def test_review_rejects_changes_after_review_without_writing(tmp_path, changed_area):
    from lingnexus.common.errors import Conflict

    app, runtime, task_id, project, staged, review = _task_workspace(tmp_path)
    try:
        target = project / "b.txt" if changed_area == "source" else staged / "b.txt"
        target.write_text("owner update", encoding="utf-8")
        with pytest.raises(Conflict):
            asyncio.run(runtime.apply(task_id, review_id=review["review_id"]))
        assert (project / "a.txt").read_text(encoding="utf-8") == "old-a"
        assert (project / "b.txt").read_text(encoding="utf-8") == (
            "owner update" if changed_area == "source" else "old-b"
        )
        assert (project / "c.txt").read_text(encoding="utf-8") == "old-c"
    finally:
        asyncio.run(runtime.aclose())
        app.close()
