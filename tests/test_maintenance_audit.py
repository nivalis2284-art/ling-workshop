"""Maintenance audit: 037/038 state, portable safety and real round trips."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from lingnexus.continuity import Continuity
from lingnexus.maintenance.restore import verify_backup
from lingnexus.models import APIFamily, ModelProfile

CONTINUITY_TABLES = (
    "continuity_work_branches",
    "continuity_work_guidance",
    "continuity_work_task_states",
    "continuity_work_episodes",
    "continuity_episode_archives",
)
PRIVATE_TABLES = ("chat_run_checkpoints", "chat_tool_effects")
PRIVATE_MARKERS = ("private-checkpoint-audit", "private-effect-audit")


def _database_rows(path, table: str) -> list[dict]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]


def _run_and_branch(app, agent_id):
    """A real 037 lifecycle with one archived predecessor and one active successor."""
    continuity = Continuity(app)
    app.continuity = continuity
    main = app.conversations.create(agent_id, "主干")["id"]
    continuity.main.bind(agent_id, main, 0)
    source = app.conversations.append_user(main, "审计来源", request_id="audit-main")
    app.conversations.edit_user(main, source["id"], "审计来源新版", request_id="audit-main-edit")
    assert _source_epoch(app, main) > 0
    branch = continuity.branch(agent_id, objective="维护审计", request_id="audit-branch")
    episode = branch["current_episode_id"]
    profile = app.profiles.save(
        "audit-profile",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="fixture",
            base_url="https://fixture.test",
        ),
        api_key="audit-secret",
    )
    app.profiles.select(profile["id"], None)
    with app.database.transaction() as db:
        run_id = "run-audit"
        db.execute(
            "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
            "conversation_id,created_at,finished_at) VALUES(?,?,?,'fingerprint',?,"
            "'completed',?,'2026-10-18T00:00:00Z','2026-10-18T00:01:00Z')",
            (run_id, agent_id, "run-audit-request", profile["id"], branch["conversation_id"]),
        )
        db.execute(
            "UPDATE continuity_work_episodes SET run_id=?,status='paused',closed_at=? "
            "WHERE id=?",
            (run_id, "2026-10-18T00:01:00Z", episode),
        )
        db.execute(
            "UPDATE continuity_work_branches SET status='paused',run_id=? WHERE id=?",
            (run_id, branch["id"]),
        )
    user = app.conversations.append_user(branch["conversation_id"], "旧阶段原文", request_id="archive-source")
    app.conversations.append_assistant(branch["conversation_id"], "已核对旧阶段", request_id="archive-answer",
                                       expected_head=user["id"])
    continuity.request_finish(branch["id"], continuity.work(branch["id"])["revision"])
    archived = continuity.returns.archive(branch["id"])
    assert archived["archive_id"]
    assert len(continuity.archives(branch["id"])) == 1
    # A real archived predecessor opens a new episode without changing its archive.
    woken = continuity.wake(branch["id"], continuity.work(branch["id"])["revision"])
    assert woken["current_episode_id"] != episode
    return run_id


def _seed_private_state(app, run_id):
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO chat_run_checkpoints(run_id,state,updated_at,assistant_message_id) "
            "VALUES(?,?,'2026-10-18T00:02:00Z',NULL)",
            (run_id, json.dumps({"marker": PRIVATE_MARKERS[0]})),
        )
        db.execute(
            "INSERT INTO chat_tool_effects(run_id,step,call_id,status,result) "
            "VALUES(?,1,'audit-effect','completed',?)",
            (run_id, json.dumps(PRIVATE_MARKERS[1])),
        )
        db.execute(
            "INSERT INTO chat_run_controls(run_id,intent,revision,updated_at) "
            "VALUES(?,'pause',7,'2026-10-18T00:03:00Z') "
            "ON CONFLICT(run_id) DO UPDATE SET intent='pause',revision=7,"
            "updated_at='2026-10-18T00:03:00Z'",
            (run_id,),
        )


def _assert_portable_work_can_wake(app, agent_id, original_path):
    """Copied branch tokens must still describe the restored source ledger."""
    branch = app.continuity.list_work(agent_id)[0]
    app.continuity.assert_fork(app.continuity.work_domain.row(branch["id"]))
    with app.database.transaction() as db:
        row = db.execute(
            "SELECT epoch FROM continuity_source_versions WHERE conversation_id=?",
            (branch["main_conversation_id"],),
        ).fetchone()
        grants = json.loads(db.execute(
            "SELECT grants FROM continuity_work_branches WHERE id=?", (branch["id"],)
        ).fetchone()[0])
    assert row is not None
    assert row[0] == grants["main_epoch"]
    with app.database.transaction() as db:
        background = json.loads(db.execute(
            "SELECT background FROM continuity_work_branches WHERE id=?", (branch["id"],)
        ).fetchone()[0])
        assets = [
            db.execute(
                "SELECT revision FROM continuity_message_assets WHERE message_id=?",
                (source["message_id"],),
            ).fetchone()[0]
            for source in background
            if source.get("message_id")
        ]
    source_ids = [source["message_id"] for source in background if source.get("message_id")]
    original = {row["message_id"]: row["revision"] for row in _database_rows(
        original_path, "continuity_message_assets"
    )}
    actual = {row["message_id"]: row["revision"] for row in _database_rows(
        app.database.path, "continuity_message_assets"
    )}
    assert assets == [original[mid] for mid in source_ids]
    assert actual == original


def _source_epoch(app, conversation_id):
    with app.database.transaction() as db:
        return db.execute(
            "SELECT epoch FROM continuity_source_versions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]


def _set_asset_revision(app, message_id):
    with app.database.transaction() as db:
        db.execute(
            "UPDATE continuity_message_assets SET revision=1 WHERE message_id=?",
            (message_id,),
        )


def test_portable_round_trip_preserves_037_and_control_classification(app, agent_id):
    run_id = _run_and_branch(app, agent_id)
    _seed_private_state(app, run_id)
    old_branch = app.continuity.list_work(agent_id)[0]
    old_head = app.conversations.append_assistant(
        old_branch["conversation_id"], "旧工作阶段", request_id="audit-phase",
        expected_head=app.conversations.read(old_branch["conversation_id"])["head_id"],
    )["id"]
    app.conversations.append_user(
        old_branch["conversation_id"], "旧阶段第二条", request_id="audit-epoch"
    )
    app.conversations.select_head(old_branch["conversation_id"], old_head)
    assert _source_epoch(app, old_branch["conversation_id"]) > 0
    export = app.database.path.parent / "portable-audit"
    manifest = app.maintenance.export_to(export)
    assert set(CONTINUITY_TABLES).issubset(manifest["tables"])
    assert "chat_run_controls" not in manifest["tables"]
    assert set(PRIVATE_TABLES).isdisjoint(manifest["tables"])
    restored = app.database.path.parent / "restored-audit" / "ling.db"
    app.maintenance.restore_from(export, restored)
    for table in CONTINUITY_TABLES:
        assert _database_rows(app.database.path, table) == _database_rows(restored, table), table
    for table in PRIVATE_TABLES:
        assert _database_rows(restored, table) == [], table
    for table in ("continuity_source_versions", "continuity_message_assets"):
        assert _database_rows(restored, table) == _database_rows(app.database.path, table), table
    from lingnexus.app import Application

    restored_app = Application(restored)
    try:
        _assert_portable_work_can_wake(restored_app, agent_id, app.database.path)
    finally:
        restored_app.close()


def test_portable_source_ledger_preserves_nonzero_message_asset_version(app, agent_id):
    _run_and_branch(app, agent_id)
    old_branch = app.continuity.list_work(agent_id)[0]
    with app.database.transaction() as db:
        background = json.loads(
            db.execute(
                "SELECT background FROM continuity_work_branches WHERE id=?", (old_branch["id"],)
            ).fetchone()[0]
        )
    message_id = background[0]["message_id"]
    _set_asset_revision(app, message_id)
    source = app.database.path.parent / "asset-ledger"
    app.maintenance.export_to(source)
    restored = app.database.path.parent / "asset-ledger-restored" / "ling.db"
    app.maintenance.restore_from(source, restored)
    assert _database_rows(restored, "continuity_message_assets") == _database_rows(
        app.database.path, "continuity_message_assets"
    )
    with closing(sqlite3.connect(restored)) as db:
        restored_revision = db.execute(
            "SELECT revision FROM continuity_message_assets WHERE message_id=?", (message_id,)
        ).fetchone()[0]
    assert restored_revision > 0


def test_old_v4_without_source_ledger_fails_before_touching_destination(app, agent_id):
    _run_and_branch(app, agent_id)
    source = app.database.path.parent / "old-v4"
    app.maintenance.export_to(source)
    for table in ("continuity_source_versions", "continuity_message_assets"):
        (source / f"{table}.json").unlink()
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for table in ("continuity_source_versions", "continuity_message_assets"):
        manifest["tables"].pop(table)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = app.database.path.parent / "preserved" / "ling.db"
    destination.parent.mkdir()
    destination.write_bytes(b"owner destination stays intact")
    with pytest.raises(ValueError, match="来源版本账"):
        app.maintenance.restore_from(source, destination)
    assert destination.read_bytes() == b"owner destination stays intact"
    assert not (destination.parent / "attachments").exists()


def test_old_v4_without_work_branches_still_restores(app, agent_id):
    app.conversations.create(agent_id, "no branch")
    source = app.database.path.parent / "old-v4-no-branch"
    app.maintenance.export_to(source)
    for table in ("continuity_source_versions", "continuity_message_assets"):
        (source / f"{table}.json").unlink()
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for table in ("continuity_source_versions", "continuity_message_assets"):
        manifest["tables"].pop(table)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restored = app.database.path.parent / "old-v4-no-branch-restored" / "ling.db"
    app.maintenance.restore_from(source, restored)
    assert restored.is_file()


@pytest.mark.parametrize(
    ("table", "key"),
    [("continuity_source_versions", "conversation_id"), ("continuity_message_assets", "message_id")],
)
def test_portable_rejects_a_partial_source_ledger(app, agent_id, table, key):
    _run_and_branch(app, agent_id)
    source = app.database.path.parent / f"partial-{table}"
    app.maintenance.export_to(source)
    path = source / f"{table}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) > 1
    rows.pop()
    content = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(content)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"][table] = {
        "sha256": hashlib.sha256(content).hexdigest(), "rows": len(rows)
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = app.database.path.parent / f"partial-{table}.db"
    with pytest.raises(ValueError, match="来源版本账"):
        app.maintenance.restore_from(source, destination)
    assert not destination.exists()


def test_backup_round_trip_keeps_private_state_and_run_controls(app, agent_id):
    run_id = _run_and_branch(app, agent_id)
    _seed_private_state(app, run_id)
    checkpoint_quiet = sqlite3.connect(app.database.path)
    checkpoint_quiet.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint_quiet.commit()
    checkpoint_quiet.close()
    source = app.database.path.parent / "whole-audit.db"
    app.maintenance.backup_to(source)
    verify_backup(source)
    restored = app.database.path.parent / "restored-whole-audit" / "ling.db"
    app.maintenance.restore_backup(source, restored)
    assert _database_rows(restored, "chat_run_controls") == _database_rows(
        app.database.path, "chat_run_controls"
    )
    assert _database_rows(restored, "chat_run_controls") == [
        {"run_id": run_id, "intent": "pause", "revision": 7,
         "updated_at": "2026-10-18T00:03:00Z"}
    ]
    assert _database_rows(app.database.path, "continuity_episode_archives") == _database_rows(
        restored, "continuity_episode_archives"
    )
    assert _database_rows(restored, "chat_run_checkpoints") == [
        {"run_id": run_id, "state": json.dumps({"marker": PRIVATE_MARKERS[0]}),
         "updated_at": "2026-10-18T00:02:00Z", "assistant_message_id": None}
    ]
    assert _database_rows(restored, "chat_tool_effects") == [
        {"run_id": run_id, "step": 1, "call_id": "audit-effect", "status": "completed",
         "result": json.dumps(PRIVATE_MARKERS[1])}
    ]
