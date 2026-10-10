import json

import pytest
from fastapi.testclient import TestClient

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict, SourceInvalid
from lingnexus.common.types import new_id, utc_now


@pytest.fixture
def maintenance(app):
    from lingnexus.memory.maintenance import MemoryMaintenance

    return MemoryMaintenance(app)


def entry(app, agent_id, kind="episodes", scope="real", text="原始摘要"):
    ref = app.history.add(agent_id, "保留的原始记录", kind="user", scope=scope, request_id=new_id())
    if kind == "episodes":
        item_id = app.memory.save_episode(
            agent_id, "原始标题", text, [ref], scope=scope, request_id=new_id()
        )
    else:
        # Explicit columns also seed legacy Knowledge.save before parent integration.
        item_id = new_id()
        with app.database.transaction() as db:
            db.execute(
                "INSERT INTO knowledge "
                "(id,agent_id,statement,kind,scope,status,request_id,fingerprint,created_at) "
                "VALUES (?,?,?,'fact',?,'active',?,'seed',?)",
                (item_id, agent_id, text, scope, new_id(), utc_now()),
            )
            db.execute("INSERT INTO knowledge_sources VALUES (?,?,?)", (item_id, ref.id, 1))
    return item_id, ref


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("episodes", {"title": "主人更正标题", "summary": "主人更正摘要"}),
        ("knowledge", {"statement": "主人更正主张"}),
    ],
)
def test_correction_is_versioned_owner_assertion_with_original_refs(
    app, agent_id, maintenance, kind, changes, app_factory
):
    item_id, ref = entry(app, agent_id, kind)
    updated = maintenance.correct(kind, item_id, expected_revision=1, **changes)
    assert updated["revision"] == 2
    assert updated["provenance"] == "owner_assertion"
    assert updated["sources"] == [ref.model_dump()]
    assert all(updated[key] == value for key, value in changes.items())
    assert app.history.read(ref)["text"] == "保留的原始记录"
    with pytest.raises(RevisionConflict):
        maintenance.correct(kind, item_id, expected_revision=1, **changes)
    reopened = app_factory(app.database.path)
    from lingnexus.memory.maintenance import MemoryMaintenance

    history = MemoryMaintenance(reopened).read(kind, item_id)["corrections"]
    assert len(history) == 1
    assert history[0]["actor"] == "owner"
    assert history[0]["revision"] == 2
    assert history[0]["created_at"]
    assert history[0]["prior"].get("summary", history[0]["prior"].get("statement")) == "原始摘要"
    assert history[0]["current"]["sources"] == [ref.model_dump()]
    reopened.close()


def test_episode_fts_replaces_old_content(app, agent_id, maintenance):
    item_id, _ = entry(app, agent_id, text="obsoleteword")
    maintenance.correct("episodes", item_id, expected_revision=1, summary="replacementword")
    assert app.memory.find_episodes(agent_id, "obsoleteword") == []
    assert app.memory.find_episodes(agent_id, "replacementword")[0]["id"] == item_id


@pytest.mark.parametrize("kind", ["episodes", "knowledge"])
def test_archive_restore_delete_preserves_raw_history_and_audit(app, agent_id, maintenance, kind):
    item_id, ref = entry(app, agent_id, kind)
    archived = maintenance.set_state(kind, item_id, action="archive", expected_revision=1)
    assert (archived["status"], archived["revision"]) == ("archived", 2)
    assert maintenance.items(agent_id, kind=kind, status="active") == []
    assert maintenance.items(agent_id, kind=kind, status="archived")[0]["id"] == item_id
    restored = maintenance.set_state(kind, item_id, action="restore", expected_revision=2)
    assert (restored["status"], restored["revision"]) == ("active", 3)
    with pytest.raises(RevisionConflict):
        maintenance.set_state(kind, item_id, action="delete", expected_revision=2)
    deleted = maintenance.set_state(kind, item_id, action="delete", expected_revision=3)
    assert (deleted["status"], deleted["revision"]) == ("deleted", 4)
    with pytest.raises(NotFound):
        maintenance.read(kind, item_id)
    assert app.history.read(ref)["text"] == "保留的原始记录"
    with app.database.transaction() as db:
        singular = "episode" if kind == "episodes" else "knowledge"
        assert (
            db.execute(
                f"SELECT count(*) FROM {singular}_sources WHERE {singular}_id=?", (item_id,)
            ).fetchone()[0]
            == 0
        )
        if kind == "episodes":
            assert (
                db.execute(
                    "SELECT count(*) FROM episode_search WHERE episode_id=?", (item_id,)
                ).fetchone()[0]
                == 0
            )
        edits = db.execute(
            "SELECT * FROM memory_edits WHERE target_id=? ORDER BY revision", (item_id,)
        ).fetchall()
        assert [row["action"] for row in edits] == ["archive", "restore", "delete"]
        assert json.loads(edits[-1]["prior"])["status"] == "active"
        assert json.loads(edits[-1]["current"])["sources"] == [ref.model_dump()]


@pytest.mark.parametrize("kind", ["episodes", "knowledge"])
@pytest.mark.parametrize(
    "invalidate", ["correct_source", "hide_branch", "stale", "foreign", "missing"]
)
def test_cannot_correct_or_restore_invalid_sources(app, agent_id, maintenance, kind, invalidate):
    item_id, ref = entry(app, agent_id, kind)
    maintenance.set_state(kind, item_id, action="archive", expected_revision=1)
    if invalidate == "correct_source":
        app.history.correct(ref, "新的原文")
    elif invalidate == "hide_branch":
        app.history.set_visible(ref, False)
        app.history.set_visible(ref, True)
    else:
        with app.database.transaction() as db:
            if invalidate == "stale":
                db.execute("UPDATE sources SET current_revision=2 WHERE id=?", (ref.id,))
            elif invalidate == "foreign":
                other = app.create_agent("另一个灵")
                db.execute("UPDATE sources SET agent_id=? WHERE id=?", (other, ref.id))
            else:
                singular = "episode" if kind == "episodes" else "knowledge"
                db.execute(f"DELETE FROM {singular}_sources WHERE {singular}_id=?", (item_id,))
    for operation in (
        lambda: maintenance.set_state(kind, item_id, action="restore", expected_revision=2),
        lambda: maintenance.correct(
            kind,
            item_id,
            expected_revision=2,
            **({"summary": "不能覆盖"} if kind == "episodes" else {"statement": "不能覆盖"}),
        ),
    ):
        with pytest.raises(SourceInvalid):
            operation()
    assert maintenance.read(kind, item_id)["revision"] == 2
    with app.database.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM memory_edits WHERE target_id=?", (item_id,)
            ).fetchone()[0]
            == 1
        )


def test_status_and_query_are_explicit_and_literal(app, agent_id, maintenance):
    first, _ = entry(app, agent_id, text="first 100%_value")
    second, _ = entry(app, agent_id, text="second 外部报告")
    entry(app, app.create_agent("另一个灵"), text="其他个体")
    assert {r["id"] for r in maintenance.items(agent_id, kind="episodes")} == {first, second}
    assert len(maintenance.items(agent_id, kind="episodes", query="%_")) == 1
    assert maintenance.items(agent_id, kind="episodes", query="not%_") == []


def test_running_recall_blocks_maintenance_without_changing_raw(app, agent_id, maintenance):
    item_id, ref = entry(app, agent_id)
    # Exercise actual persisted runs without starting a model or browser.
    with app.database.transaction() as db:
        db.execute("INSERT INTO model_profiles VALUES ('profile','测试','{}')")
        db.execute(
            "INSERT INTO chat_runs (id,agent_id,request_id,fingerprint,profile_id,status,"
            "user_source_id,user_source_revision,context_sources,created_at,context_manifest) "
            "VALUES ('run',?,'req','fp',?,'running',?,1,'[]',?,?)",
            (
                agent_id,
                "profile",
                ref.id,
                utc_now(),
                json.dumps({"recall_sources": [ref.model_dump()]}),
            ),
        )
    with pytest.raises(Conflict):
        maintenance.correct("episodes", item_id, expected_revision=1, summary="新摘要")
    with pytest.raises(Conflict):
        maintenance.set_state("episodes", item_id, expected_revision=1, action="delete")
    assert maintenance.read("episodes", item_id)["revision"] == 1
    assert app.history.read(ref)["revision"] == 1


def test_api_owner_boundary_and_strict_revision(app, agent_id, maintenance, tmp_path):
    from lingnexus.api.app import create_app
    from lingnexus.api.memory import router

    server = create_app(
        data_dir=tmp_path, token="memory-test-owner", frontend_dir=tmp_path / "no-frontend"
    )
    server.include_router(router)
    server.state.services = app
    client = TestClient(server, base_url="http://localhost:8765")
    item_id, _ = entry(app, agent_id)
    headers = {"Authorization": "Bearer memory-test-owner"}
    path = f"/memory/episodes/{item_id}"
    for method, endpoint, body in [
        ("GET", f"/memory/{agent_id}/items", None),
        ("GET", path, None),
        ("PATCH", path, {"expected_revision": 1, "summary": "更正"}),
        ("POST", path + "/state", {"expected_revision": 1, "action": "delete"}),
    ]:
        assert client.request(method, endpoint, json=body).status_code == 401
        assert (
            client.request(
                method, endpoint, json=body, headers=headers | {"Origin": "https://foreign.invalid"}
            ).status_code
            == 403
        )
    for body in (
        {"summary": "更正"},
        {"expected_revision": True, "summary": "更正"},
        {"expected_revision": 1, "summary": " "},
        {"expected_revision": 1},
        {"expected_revision": 1, "summary": None},
        {"expected_revision": 1, "summary": "更正", "actor": "forged"},
    ):
        assert client.patch(path, json=body, headers=headers).status_code == 422
    result = client.patch(path, json={"expected_revision": 1, "summary": "更正"}, headers=headers)
    assert result.status_code == 200
    assert result.json()["revision"] == 2
    assert (
        client.post(
            path + "/state", json={"expected_revision": 1, "action": "delete"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.get(f"/memory/{agent_id}/items?kind=episodes", headers=headers).json()[0]["summary"]
        == "更正"
    )
    assert (
        client.get(
            f"/memory/{agent_id}/items?kind=episodes&status=invalid_status", headers=headers
        ).status_code
        == 422
    )


@pytest.mark.parametrize("kind", ["episodes", "knowledge"])
@pytest.mark.parametrize("action", ["correct", "archive", "delete"])
def test_memory_marker_rejects_changed_derived_content_with_unchanged_sources(
    app, agent_id, maintenance, kind, action
):
    item_id, ref = entry(app, agent_id, kind)
    marker = maintenance.snapshot_marker(kind, item_id)
    assert marker == {"kind": kind, "id": item_id, "revision": 1}
    maintenance.validate_markers(agent_id, [marker])
    if action == "correct":
        changes = {"summary": "更正摘要"} if kind == "episodes" else {"statement": "更正主张"}
        maintenance.correct(kind, item_id, expected_revision=1, **changes)
    else:
        maintenance.set_state(kind, item_id, expected_revision=1, action=action)
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(agent_id, [marker])
    assert app.history.read(ref)["revision"] == 1


def test_marker_checks_agent_scope_source_and_status(app, agent_id, maintenance):
    item_id, ref = entry(app, agent_id)
    marker = maintenance.snapshot_marker("episodes", item_id)
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(app.create_agent("其他个体"), [marker])
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(agent_id, [marker | {"revision": True}])
    app.history.set_visible(ref, False)
    with pytest.raises(SourceInvalid):
        maintenance.validate_markers(agent_id, [marker])


def test_failed_audit_rolls_back_content_and_fts(app, agent_id, maintenance):
    import sqlite3

    item_id, ref = entry(app, agent_id, text="originalword")
    with app.database.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON memory_edits "
            "BEGIN SELECT RAISE(ABORT,'audit unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        maintenance.correct("episodes", item_id, expected_revision=1, summary="replacementword")
    assert maintenance.read("episodes", item_id)["revision"] == 1
    assert app.memory.find_episodes(agent_id, "originalword")[0]["id"] == item_id
    assert app.memory.find_episodes(agent_id, "replacementword") == []
    assert app.history.read(ref)["text"] == "保留的原始记录"


def test_multiple_corrections_append_prior_snapshots(app, agent_id, maintenance):
    item_id, _ = entry(app, agent_id, "knowledge")
    maintenance.correct("knowledge", item_id, expected_revision=1, statement="第一次更正")
    item = maintenance.correct("knowledge", item_id, expected_revision=2, statement="第二次更正")
    first, second = item["corrections"]
    assert first["prior"]["statement"] == "原始摘要"
    assert first["current"]["statement"] == "第一次更正"
    assert second["prior"]["statement"] == "第一次更正"
    assert second["current"]["statement"] == "第二次更正"
    assert [edit["revision"] for edit in item["corrections"]] == [2, 3]


def test_migration_upgrades_existing_knowledge_and_preserves_raw(tmp_path):
    import sqlite3
    from pathlib import Path

    import lingnexus.storage.db as storage
    from lingnexus.storage.db import Database

    path = tmp_path / "legacy.db"
    migrations = Path(storage.__file__).parent / "migrations"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)")
        for migration in sorted(migrations.glob("*.sql")):
            if migration.name >= "022_memory_maintenance.sql":
                break
            db.executescript(migration.read_text(encoding="utf-8"))
            db.execute("INSERT INTO schema_migrations VALUES (?)", (migration.name,))
        db.execute("INSERT INTO agents VALUES ('agent','灵','now')")
        db.execute("INSERT INTO sources VALUES ('source','agent','user','real','r','fp',1,'now')")
        db.execute("INSERT INTO source_revisions VALUES ('source',1,'原始历史','now')")
        db.execute(
            "INSERT INTO knowledge VALUES ('knowledge','agent','原始主张','fact','real',"
            "'active','k','fp','now')"
        )
        db.execute("INSERT INTO knowledge_sources VALUES ('knowledge','source',1)")
    database = Database(path)
    with database.transaction() as db:
        assert db.execute("SELECT revision FROM knowledge").fetchone()[0] == 1
        assert db.execute("SELECT content FROM source_revisions").fetchone()[0] == "原始历史"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    Database(path)  # Reopening must not apply ALTER TABLE again.
