import json
import os
from pathlib import Path

import pytest

from lingnexus.desktop.restore import apply_pending_restore
from lingnexus.maintenance.restore import RestoreQueue


@pytest.fixture(params=["backup", "export"])
def prepared(request, app, agent_id, tmp_path):
    from test_task_assets_backup import create_paused_task

    tid, snapshots, _ = create_paused_task(app, agent_id, tmp_path)
    old = app.attachments.add(agent_id, "old.txt", b"original attachment")
    source = tmp_path / "source" / ("backup.db" if request.param == "backup" else "export")
    if request.param == "backup":
        app.maintenance.backup_to(source)
    else:
        app.maintenance.export_to(source)
    newer = app.attachments.add(agent_id, "new.txt", b"newer history must survive rollback")
    (snapshots.path(tid) / "answer.txt").write_bytes(b"newer task artifact\n")
    queue = RestoreQueue(app)
    plan = queue.prepare(request.param, str(source))
    return queue, plan, old, newer, source


def task_directory(app):
    with app.database.transaction() as db:
        tid = db.execute("SELECT id FROM tasks").fetchone()[0]
    return app.database.path.parent / "task-data" / tid


def test_prepare_and_commit_do_not_touch_current_database(prepared, app):
    queue, plan, old, newer, _ = prepared
    assert plan["summary"]["attachments"] == 1
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"
    with pytest.raises(ValueError):
        queue.commit(plan["plan_id"], confirmed=False)
    with pytest.raises(ValueError):
        queue.commit("../outside", confirmed=True)
    queued = queue.commit(plan["plan_id"], confirmed=True)
    assert queued["status"] == "queued"
    assert queue.status()["plan_id"] == plan["plan_id"]
    assert app.attachments.read_bytes(old["id"]) == b"original attachment"
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"


def test_next_startup_applies_and_preserves_current_database_and_attachments(
    prepared, app, app_factory
):
    queue, plan, old, newer, _ = prepared
    queue.commit(plan["plan_id"], confirmed=True)
    directory = app.database.path.parent
    app.close()
    result = apply_pending_restore(directory)
    assert result["status"] == "applied"
    restored = app_factory(directory / "ling.db")
    task = task_directory(restored)
    assert (task / "workspace/answer.txt").read_bytes() == b"changed answer\n"
    assert (task / "baseline/answer.txt").read_bytes() == b"original answer\n"
    assert (task / "workspace/download.bin").read_bytes() == b"\x00\xffdownloaded artifact"
    assert (task / "workspace/.deps/python/fixture_dep/__init__.py").is_file()
    assert restored.attachments.read_bytes(old["id"]) == b"original attachment"
    with restored.database.transaction() as db:
        assert not db.execute("SELECT 1 FROM attachments WHERE id=?", (newer["id"],)).fetchone()
    rollback = Path(result["rollback_backup"])
    restored.maintenance.restore_backup(rollback, directory / "rollback-check" / "ling.db")
    previous = app_factory(directory / "rollback-check" / "ling.db")
    assert (
        task_directory(previous) / "workspace/answer.txt"
    ).read_bytes() == b"newer task artifact\n"
    assert previous.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"
    assert apply_pending_restore(directory)["status"] == "idle"


def test_changed_source_is_rejected_without_replacing_current_data(prepared, app):
    queue, plan, _, newer, source = prepared
    queue.commit(plan["plan_id"], confirmed=True)
    target = source if source.is_file() else source / "agents.json"
    target.write_bytes(b"tampered after preview")
    with pytest.raises(ValueError):
        apply_pending_restore(app.database.path.parent)
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"


@pytest.mark.parametrize("crash", [False, True])
def test_partial_replacement_rolls_back_and_recovers_after_interruption(
    prepared, app, monkeypatch, crash
):
    queue, plan, _, newer, _ = prepared
    queue.commit(plan["plan_id"], confirmed=True)
    directory = app.database.path.parent
    replace = os.replace
    failed = False

    def interrupt(source, target):
        nonlocal failed
        if (
            Path(target) == directory / "ling.db"
            and Path(source).parent.name == "incoming"
            and not failed
        ):
            failed = True
            if crash:
                raise KeyboardInterrupt("power loss")
            raise OSError("disk failure")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", interrupt)
    if crash:
        with pytest.raises(KeyboardInterrupt):
            apply_pending_restore(directory)
        monkeypatch.setattr(os, "replace", replace)
        result = apply_pending_restore(directory)
    else:
        result = apply_pending_restore(directory)
    assert result["status"] == "rolled_back"
    assert (task_directory(app) / "workspace/answer.txt").read_bytes() == b"newer task artifact\n"
    assert Path(result["rollback_backup"]).exists()
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"
    assert apply_pending_restore(directory)["status"] == "idle"


@pytest.mark.parametrize(
    "value",
    [
        {"plan_id": "../../elsewhere"},
        [],
        {
            "format": "lingnexus-restore-queue",
            "version": 1,
            "plan_id": "a" * 32,
            "path": "../victim",
        },
    ],
)
def test_malformed_queue_fails_closed(app, agent_id, value):
    path = app.database.path.parent / "pending-restore.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        apply_pending_restore(app.database.path.parent)
    assert app.identity.current(agent_id).revision == 0


def test_tampered_staging_is_rejected(prepared, app):
    queue, plan, _, newer, _ = prepared
    staged = app.database.path.parent / "restore-staging" / plan["plan_id"] / "ling.db"
    staged.write_bytes(b"tampered staging")
    with pytest.raises(ValueError):
        queue.commit(plan["plan_id"], confirmed=True)
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"


def test_failure_copying_incoming_is_recoverable_without_replacing_current_data(
    prepared, app, monkeypatch
):
    import shutil

    queue, plan, _, newer, _ = prepared
    queue.commit(plan["plan_id"], confirmed=True)
    monkeypatch.setattr(
        shutil, "copy2", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )
    result = apply_pending_restore(app.database.path.parent)
    assert result["status"] == "rolled_back"
    assert queue.status()["status"] == "idle"
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"


def test_restore_rejects_wal_added_to_staging(prepared, app):
    queue, plan, _, newer, _ = prepared
    stage = app.database.path.parent / "restore-staging" / plan["plan_id"]
    (stage / "ling.db-wal").write_bytes(b"unconfirmed sidecar")
    with pytest.raises(ValueError):
        queue.commit(plan["plan_id"], confirmed=True)
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"


def test_symlinked_staging_or_current_attachment_tree_is_rejected(prepared, app, tmp_path):
    queue, plan, _, newer, _ = prepared
    stage = tmp_path / "restore-staging" / plan["plan_id"]
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / "sentinel").write_bytes(b"untouched")
    try:
        (stage / "attachments" / "linked").symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")
    with pytest.raises(ValueError):
        queue.commit(plan["plan_id"], confirmed=True)
    assert (victim / "sentinel").read_bytes() == b"untouched"
    assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"


@pytest.mark.parametrize(
    "point",
    [
        "old_db",
        "old_attachments",
        "old_task_data",
        "new_attachments",
        "new_task_data",
        "new_db",
        "applied",
        "intent_archive",
        "journal_archive",
    ],
)
def test_crash_after_each_publication_step_is_idempotently_recovered(
    prepared, app, monkeypatch, point
):
    queue, plan, old, newer, _ = prepared
    directory = app.database.path.parent
    extra = directory / "attachments" / "unreferenced-history.txt"
    extra.write_bytes(b"keep original history even without a database reference")
    task = task_directory(app)
    task_extra = task / "workspace/unreferenced-history.bin"
    task_extra.write_bytes(b"keep old task assets")
    queue.commit(plan["plan_id"], confirmed=True)
    replace = os.replace
    interrupted = False

    def crash_after(source, target):
        nonlocal interrupted
        source, target = Path(source), Path(target)
        phase = None
        if target == directory / "restore-journal.json":
            phase = json.loads(source.read_text(encoding="utf-8"))["phase"]
        matches = {
            "old_db": source == directory / "ling.db" and target.parent.name == "previous",
            "old_attachments": source == directory / "attachments"
            and target.parent.name == "previous",
            "old_task_data": source == directory / "task-data" and target.parent.name == "previous",
            "new_attachments": source.parent.name == "incoming"
            and target == directory / "attachments",
            "new_task_data": source.parent.name == "incoming" and target == directory / "task-data",
            "new_db": source.parent.name == "incoming" and target == directory / "ling.db",
            "applied": phase == "applied",
            "intent_archive": source == directory / "pending-restore.json",
            "journal_archive": source == directory / "restore-journal.json",
        }
        result = replace(source, target)
        if matches[point] and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("interrupted after atomic rename")
        return result

    monkeypatch.setattr(os, "replace", crash_after)
    with pytest.raises(KeyboardInterrupt):
        apply_pending_restore(directory)
    monkeypatch.setattr(os, "replace", replace)
    result = apply_pending_restore(directory)
    assert app.attachments.read_bytes(old["id"]) == b"original attachment"
    if point in {"applied", "intent_archive", "journal_archive"}:
        assert result["status"] in {"applied", "idle"}
        originals = directory / "restore-staging" / plan["plan_id"] / "previous" / "attachments"
        assert (
            originals / extra.name
        ).read_bytes() == b"keep original history even without a database reference"
        previous_task = originals.parent / "task-data" / task.name
        assert (
            previous_task / "workspace/unreferenced-history.bin"
        ).read_bytes() == b"keep old task assets"
        assert (task / "workspace/answer.txt").read_bytes() == b"changed answer\n"
    else:
        assert result["status"] == "rolled_back"
        assert app.attachments.read_bytes(newer["id"]) == b"newer history must survive rollback"
        assert extra.read_bytes() == b"keep original history even without a database reference"
        assert task_extra.read_bytes() == b"keep old task assets"
        assert (task / "workspace/answer.txt").read_bytes() == b"newer task artifact\n"
    assert apply_pending_restore(directory)["status"] == "idle"


def test_changed_staged_task_assets_cannot_be_queued(prepared, app):
    queue, plan, _, _, _ = prepared
    task = task_directory(app)
    stage = app.database.path.parent / "restore-staging" / plan["plan_id"]
    asset = stage / "task-data" / task.name / "workspace/download.bin"
    assert asset.is_file()
    asset.write_bytes(b"changed after preview")
    with pytest.raises(ValueError):
        queue.commit(plan["plan_id"], confirmed=True)
    assert (task / "workspace/answer.txt").read_bytes() == b"newer task artifact\n"


def test_attachment_path_traversal_in_backup_manifest_is_rejected(app, agent_id, tmp_path):
    app.attachments.add(agent_id, "file.txt", b"content")
    source = tmp_path / "backup.db"
    app.maintenance.backup_to(source)
    manifest_path = Path(str(source) + ".assets") / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["attachments"][0]["blob_name"] = "../../victim.txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"must survive")
    with pytest.raises(ValueError):
        RestoreQueue(app).prepare("backup", str(source))
    assert victim.read_bytes() == b"must survive"
    assert not (tmp_path / "pending-restore.json").exists()
