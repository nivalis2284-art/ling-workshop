import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(params=["portable-restore", "backup-restore", "backup"])
def database_writer(request, app, agent_id, tmp_path):
    if request.param == "portable-restore":
        source = tmp_path / "export"
        app.maintenance.export_to(source)
        return lambda target: app.maintenance.restore_from(source, target)
    if request.param == "backup-restore":
        source = tmp_path / "backup.db"
        app.maintenance.backup_to(source)
        return lambda target: app.maintenance.restore_backup(source, target)
    return app.maintenance.backup_to


@pytest.mark.parametrize("with_sidecars", [True, False])
def test_invalid_portable_restore_preserves_preexisting_sidecars(
    app, agent_id, tmp_path, with_sidecars
):
    source = tmp_path / "export"
    app.maintenance.export_to(source)
    table = source / "agents.json"
    rows = json.loads(table.read_text(encoding="utf-8"))
    rows[0]["unexpected_field"] = "invalid schema"
    data = json.dumps(rows).encode("utf-8")
    table.write_bytes(data)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"]["agents"]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "restored.db"
    sentinels = {
        Path(str(target) + "-wal"): b"existing WAL must survive",
        Path(str(target) + "-shm"): b"existing SHM must survive",
    }
    if not with_sidecars:
        sentinels = {}
    for path, content in sentinels.items():
        path.write_bytes(content)

    with pytest.raises(FileExistsError if with_sidecars else ValueError):
        app.maintenance.restore_from(source, target)

    assert not target.exists()
    for path, content in sentinels.items():
        assert path.exists(), f"Restore deleted an unowned sidecar: {path}"
        assert path.read_bytes() == content
    assert not set(tmp_path.glob("restored.db*")) - set(sentinels)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_database_writes_reject_existing_destination_files(database_writer, tmp_path, suffix):
    target = tmp_path / "destination" / "restored.db"
    target.parent.mkdir()
    sentinel = Path(str(target) + suffix)
    sentinel.write_bytes(b"owned by someone else")

    with pytest.raises(FileExistsError):
        database_writer(target)

    assert sentinel.read_bytes() == b"owned by someone else"
    assert set(target.parent.iterdir()) == {sentinel}


@pytest.mark.parametrize("suffix", ["", "-wal"])
def test_database_writes_preserve_file_created_during_sqlite_work(
    database_writer, tmp_path, monkeypatch, suffix
):
    target = tmp_path / "destination" / "restored.db"
    sentinel = Path(str(target) + suffix)
    connect = sqlite3.connect
    injected = False

    def connect_with_concurrent_sidecar(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            sentinel.write_bytes(b"concurrent WAL must survive")
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_concurrent_sidecar)
    with pytest.raises(FileExistsError):
        database_writer(target)

    assert sentinel.read_bytes() == b"concurrent WAL must survive"
    assert set(target.parent.iterdir()) == {sentinel}


@pytest.mark.parametrize("copy_error", [True, False])
def test_database_publication_preserves_concurrent_sidecars(
    database_writer, tmp_path, monkeypatch, copy_error
):
    target = tmp_path / "destination" / "restored.db"
    sentinel = Path(str(target) + "-shm")
    copy = shutil.copyfileobj

    def copy_with_concurrent_sidecar(source, destination, *args, **kwargs):
        sentinel.write_bytes(b"concurrent SHM must survive")
        if copy_error:
            destination.write(source.read(64))
            raise OSError("injected copy failure")
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", copy_with_concurrent_sidecar)
    with pytest.raises(OSError if copy_error else FileExistsError):
        database_writer(target)

    assert sentinel.read_bytes() == b"concurrent SHM must survive"
    assert not target.exists()
    assert not (Path(str(target) + ".assets") / "manifest.json").exists()
    assert not list(target.parent.glob(".lingnexus-*"))


def test_failed_backup_has_no_completion_manifest(app, agent_id, tmp_path):
    item = app.attachments.add(agent_id, "original.txt", b"attachment content")
    with app.database.transaction() as connection:
        row = connection.execute(
            "SELECT blob_name FROM attachments WHERE id=?", (item["id"],)
        ).fetchone()
    (tmp_path / "attachments" / row["blob_name"]).write_bytes(b"corrupt")
    destination = tmp_path / "backup.db"

    with pytest.raises(ValueError):
        app.maintenance.backup_to(destination)

    assert not (tmp_path / "backup.db.assets" / "manifest.json").exists()
    with pytest.raises(FileNotFoundError):
        app.maintenance.restore_backup(destination, tmp_path / "rejected.db")
    assert not (tmp_path / "rejected.db").exists()


def test_export_restore_preserves_sources_proposals_and_state(app, agent_id, tmp_path, app_factory):
    ref = app.history.add(
        agent_id, "原始经历", kind="user", scope="real", request_id="export-source"
    )
    p = app.identity.propose(
        agent_id,
        kind="identity",
        key="identity.description",
        text="当前自我",
        sources=[ref],
        base_revision=0,
        request_id="export-proposal",
    )
    app.identity.decide(p["id"], "approve", expected_revision=0)
    eid = app.memory.save_episode(agent_id, "事件", "摘要", [ref], request_id="export-episode")
    export = tmp_path / "export"
    app.maintenance.export_to(export)
    restored_path = tmp_path / "restored.db"
    app.maintenance.restore_from(export, restored_path)
    restored = app_factory(restored_path)
    assert restored.identity.current(agent_id).entries == {"identity.description": "当前自我"}
    assert restored.memory.read_episode(eid)["sources"] == [ref.model_dump()]
    assert restored.history.read(ref)["text"] == "原始经历"
    assert restored.identity.list_proposals(agent_id)[0]["status"] == "applied_owner"


def test_tampered_export_is_rejected_without_creating_target(app, agent_id, tmp_path):
    folder = tmp_path / "export"
    app.maintenance.export_to(folder)
    table = folder / "agents.json"
    table.write_text("[]", encoding="utf-8")
    target = tmp_path / "untouched.db"
    with pytest.raises(ValueError):
        app.maintenance.restore_from(folder, target)
    assert not target.exists()
    assert app.identity.current(agent_id).revision == 0


def test_restore_rejects_path_traversal_in_manifest(app, agent_id, tmp_path):
    folder = tmp_path / "export"
    app.maintenance.export_to(folder)
    path = folder / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["tables"]["../../outside"] = {"sha256": "fake", "rows": 0}
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        app.maintenance.restore_from(folder, tmp_path / "rejected.db")


def test_online_backup_has_consistent_current_state(app, agent_id, tmp_path, app_factory):
    backup = tmp_path / "backup.db"
    app.maintenance.backup_to(backup)
    restored = app_factory(backup)
    assert restored.identity.current(agent_id).revision == 0
    with pytest.raises(FileExistsError):
        app.maintenance.backup_to(backup)


def test_export_restore_includes_original_attachment(app, agent_id, tmp_path, app_factory):
    item = app.attachments.add(agent_id, "source.txt", "必须保留的原文".encode())
    folder = tmp_path / "export"
    app.maintenance.export_to(folder)
    target = tmp_path / "separate" / "restored.db"
    app.maintenance.restore_from(folder, target)
    restored = app_factory(target)
    assert restored.attachments.read_bytes(item["id"]) == "必须保留的原文".encode()


def test_corrupt_attachment_export_is_rejected_before_restore(app, agent_id, tmp_path):
    app.attachments.add(agent_id, "source.txt", b"original")
    folder = tmp_path / "export"
    app.maintenance.export_to(folder)
    next((folder / "attachments").iterdir()).write_bytes(b"tampered")
    target = tmp_path / "restored" / "ling.db"
    with pytest.raises(ValueError):
        app.maintenance.restore_from(folder, target)
    assert not target.exists()


def test_backup_restore_moves_database_and_attachment_together(
    app, agent_id, tmp_path, app_factory
):
    item = app.attachments.add(agent_id, "original.txt", b"retained attachment")
    backup = tmp_path / "backups" / "backup.db"
    app.maintenance.backup_to(backup)
    target = tmp_path / "restored" / "ling.db"
    app.maintenance.restore_backup(backup, target)
    restored = app_factory(target)
    assert restored.attachments.read_bytes(item["id"]) == b"retained attachment"
    assert restored.identity.current(agent_id).revision == 0
