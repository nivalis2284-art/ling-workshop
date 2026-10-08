"""Task continuity uses real snapshots and temporary files, without model calls."""

import hashlib
import json
import shutil
import sqlite3
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from lingnexus.models import ModelProfile
from lingnexus.tasks.store import TaskStore
from lingnexus.tasks.workspace import Snapshots


def create_paused_task(app, agent_id, directory):
    project = directory / "project"
    project.mkdir()
    (project / "answer.txt").write_bytes(b"original answer\n")
    (project / "removed.txt").write_bytes(b"original deleted file\n")
    app.permissions.configure(agent_id, readable=[project], writable=[project], expected_revision=0)
    profile = app.profiles.save(
        "offline fixture",
        ModelProfile(
            api_family="openai_chat_completions",
            model="fixture",
            base_url="https://fixture.invalid",
        ),
    )["id"]
    tid = str(uuid4())
    store = TaskStore(app.database)
    snapshots = Snapshots(app, store)
    baseline = snapshots.create(tid, agent_id, project)
    state = {
        "baseline": baseline,
        "environment": "python",
        "phase": "ready",
        "payload": {"messages": [{"role": "user", "content": "continue fixture"}]},
        "pending": [],
        "checked": {},
        "writes": {},
        "verified": False,
    }
    store.insert(
        tid,
        {
            "agent_id": agent_id,
            "objective": "offline continuity",
            "request_id": "fixture-task",
            "workspace": str(project),
        },
        "fixture-fingerprint",
        profile,
        "fixture-image",
        40,
        state,
    )
    store.status(tid, "paused")
    root = snapshots.path(tid)
    (root / "answer.txt").write_bytes(b"changed answer\n")
    (root / "removed.txt").unlink()
    (root / "download.bin").write_bytes(b"\x00\xffdownloaded artifact")
    for name, data in {
        ".deps/python/fixture_dep/__init__.py": b"VALUE = 42\n",
        "node_modules/fixture_dep/index.js": b"module.exports = 42;\n",
        ".home/.npm-globalrc": b"",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / ".tmp" / "scratch.txt").write_bytes(b"disposable scratch")
    return tid, snapshots, state


@pytest.fixture
def paused_task(app, agent_id, tmp_path):
    return create_paused_task(app, agent_id, tmp_path)


@pytest.fixture(params=["backup", "export"])
def task_archive(request, app, tmp_path):
    kind = request.param
    source = tmp_path / "archive" / ("backup.db" if kind == "backup" else "export")
    save = app.maintenance.backup_to if kind == "backup" else app.maintenance.export_to
    restore = app.maintenance.restore_backup if kind == "backup" else app.maintenance.restore_from
    assets = Path(str(source) + ".assets") if kind == "backup" else source
    return kind, source, assets, save, restore


def test_paused_task_restores_exact_changes_artifact_baseline_and_dependencies(
    app, paused_task, task_archive, tmp_path, app_factory
):
    tid, snapshots, state = paused_task
    expected = snapshots.changes(tid)["files"]
    _, source, assets, save, restore = task_archive
    save(source)
    target = tmp_path / "fresh" / "ling.db"
    restore(source, target)
    restored = app_factory(target)
    store = TaskStore(restored.database)
    recovered = Snapshots(restored, store)
    assert store.get(tid)["status"] == "paused"
    assert store.load(tid) == state
    assert recovered.changes(tid)["files"] == expected
    assert recovered.artifact(tid, "download.bin") == b"\x00\xffdownloaded artifact"
    base = target.parent / "task-data" / tid / "baseline"
    assert (base / "answer.txt").read_bytes() == b"original answer\n"
    assert (base / "removed.txt").read_bytes() == b"original deleted file\n"
    root = recovered.path(tid)
    assert (root / ".deps/python/fixture_dep/__init__.py").read_bytes() == b"VALUE = 42\n"
    assert (root / "node_modules/fixture_dep/index.js").read_bytes() == b"module.exports = 42;\n"
    assert (root / ".home/.npm-globalrc").is_file()
    assert (root / ".tmp").is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o777
    assert stat.S_IMODE(base.stat().st_mode) == 0o777
    assert stat.S_IMODE((root / ".tmp").stat().st_mode) == 0o777
    assert stat.S_IMODE((base / "answer.txt").stat().st_mode) == 0o666
    assert not (root / ".tmp/scratch.txt").exists()
    manifest = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("version", manifest.get("schema_version")) == 2
    meta = manifest["task_assets"]["tasks"][tid]["files"]["workspace/download.bin"]
    assert meta["sha256"] == hashlib.sha256(b"\x00\xffdownloaded artifact").hexdigest()
    assert manifest["task_assets"]["excluded"] == ["workspace/.tmp"]


@pytest.mark.parametrize("status", ["running", "queued"])
def test_manual_archive_rejects_tasks_that_can_write(app, paused_task, task_archive, status):
    tid, _, _ = paused_task
    with app.database.transaction() as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    _, source, assets, save, _ = task_archive
    with pytest.raises(ValueError, match="[Pp]ause|任务"):
        save(source)
    assert not source.exists()
    assert not assets.exists()


@pytest.mark.parametrize("damage", ["missing", "tampered", "manifest_missing", "traversal"])
def test_incomplete_or_tampered_task_assets_never_restore_successfully(
    app, paused_task, task_archive, tmp_path, damage
):
    tid, _, _ = paused_task
    _, source, assets, save, restore = task_archive
    save(source)
    file = assets / "task-data" / tid / "workspace/download.bin"
    if damage == "missing":
        file.unlink()
    elif damage == "tampered":
        file.write_bytes(b"corrupt")
    else:
        path = assets / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if damage == "manifest_missing":
            del manifest["task_assets"]
        else:
            manifest["task_assets"]["tasks"][tid]["files"]["../victim"] = {
                "sha256": hashlib.sha256(b"keep").hexdigest(),
                "size": 4,
                "mode": 0o600,
            }
        path.write_text(json.dumps(manifest), encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    target = tmp_path / "fresh/ling.db"
    with pytest.raises((ValueError, OSError)):
        restore(source, target)
    assert not target.exists()
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize("has_task", [False, True])
def test_legacy_v1_accepts_no_task_data_but_rejects_missing_task_assets(
    app, agent_id, task_archive, tmp_path, has_task
):
    if has_task:
        create_paused_task(app, agent_id, tmp_path)
    kind, source, assets, save, restore = task_archive
    save(source)
    path = assets / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version" if kind == "backup" else "schema_version"] = 1
    manifest.pop("task_assets", None)
    if (assets / "task-data").exists():
        shutil.rmtree(assets / "task-data")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "legacy/ling.db"
    if has_task:
        with pytest.raises(ValueError, match="[Tt]ask|任务"):
            restore(source, target)
        assert not target.exists()
    else:
        restore(source, target)
        assert target.is_file()


def test_restore_preserves_preexisting_task_directories(app, paused_task, task_archive, tmp_path):
    tid, _, _ = paused_task
    _, source, _, save, restore = task_archive
    save(source)
    target = tmp_path / "fresh/ling.db"
    sentinel = target.parent / "task-data" / tid / "workspace/old.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"unowned old asset")
    with pytest.raises(FileExistsError):
        restore(source, target)
    assert sentinel.read_bytes() == b"unowned old asset"
    assert not target.exists()


@pytest.mark.parametrize("limit", ["MAX_FILE", "MAX_TOTAL", "MAX_ENTRIES"])
def test_task_asset_limits_reject_incomplete_archives(
    paused_task, task_archive, monkeypatch, limit
):
    from lingnexus.maintenance import assets as task_assets

    monkeypatch.setattr(task_assets, limit, 1)
    _, source, assets, save, _ = task_archive
    with pytest.raises(ValueError):
        save(source)
    assert not (assets / "manifest.json").exists()


@pytest.mark.parametrize("effect", ["task_review", "dependency"])
def test_archive_rejects_active_writes_even_when_task_is_paused(
    app, paused_task, task_archive, effect
):
    tid, snapshots, _ = paused_task
    with app.database.transaction() as db:
        if effect == "task_review":
            rid = snapshots.changes(tid)["review_id"]
            db.execute("UPDATE task_reviews SET status='applying' WHERE id=?", (rid,))
        else:
            db.execute(
                "INSERT INTO dependency_plans(id,task_id,request_id,fingerprint,plan,"
                "intent_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    tid,
                    "fixture-dependency",
                    "fixture",
                    "{}",
                    "fixture-intent",
                    "running",
                    "fixture-time",
                    "fixture-time",
                ),
            )
    _, source, assets, save, _ = task_archive
    with pytest.raises(ValueError):
        save(source)
    assert not source.exists()
    assert not (assets / "manifest.json").exists()


def test_legacy_v1_without_task_tables_remains_restorable(
    app, agent_id, task_archive, tmp_path, app_factory
):
    kind, source, assets, save, restore = task_archive
    save(source)
    path = assets / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version" if kind == "backup" else "schema_version"] = 1
    manifest.pop("task_assets")
    tables = [
        "dependency_plans",
        "task_reviews",
        "task_events",
        "task_steps",
        "task_checkpoints",
        "tasks",
    ]
    if kind == "backup":
        with sqlite3.connect(source) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            for table in tables:
                db.execute(f'DROP TABLE "{table}"')
            db.execute(
                "DELETE FROM schema_migrations WHERE version IN "
                "('015_tasks.sql','023_dependencies.sql')"
            )
        db.close()
        manifest["database_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        for table in tables:
            del manifest["tables"][table]
            (source / f"{table}.json").unlink()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "legacy/ling.db"
    restore(source, target)
    restored = app_factory(target)
    assert restored.identity.current(agent_id).revision == 0
    with restored.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("crash", [False, True])
def test_failed_db_publication_removes_only_owned_task_assets(
    paused_task, task_archive, tmp_path, monkeypatch, crash
):
    tid, _, _ = paused_task
    _, source, _, save, restore = task_archive
    save(source)
    target = tmp_path / "fresh/ling.db"
    sentinel = target.parent / "task-data" / tid / "workspace/concurrent.txt"

    def interrupted_copy(*args, **kwargs):
        assert (sentinel.parent / "download.bin").is_file()
        sentinel.write_bytes(b"concurrent owner asset")
        raise KeyboardInterrupt("interrupted") if crash else OSError("disk full")

    monkeypatch.setattr(shutil, "copyfileobj", interrupted_copy)
    with pytest.raises(KeyboardInterrupt if crash else OSError):
        restore(source, target)
    assert not target.exists()
    assert sentinel.read_bytes() == b"concurrent owner asset"
    assert not (sentinel.parent / "download.bin").exists()
    assert not (sentinel.parent.parent / "baseline").exists()


def test_task_asset_junction_is_rejected_without_following_it(paused_task, task_archive, tmp_path):
    import os
    import subprocess

    tid, snapshots, _ = paused_task
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"do not follow")
    link = snapshots.path(tid) / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        _, source, assets, save, _ = task_archive
        with pytest.raises(ValueError):
            save(source)
        assert not (assets / "manifest.json").exists()
        assert (outside / "sentinel.txt").read_bytes() == b"do not follow"
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
