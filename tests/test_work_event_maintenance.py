"""Maintenance compatibility for the M2 work-event tables (031-033).

The portable export is the owner's migration path, so these properties are
locked here:

* version 3 carries the M2 tables and declares a base64 encoding for byte
  columns, and a restored database holds the same chunk bytes as the original;
* an older export (v1/v2) predates both task assets and these tables, still
  restores, and a whole-database snapshot taken before them still verifies —
  while a ledger that claims a migration whose tables are missing is rejected;
* private native replay material never enters a portable export, and safe
  record bodies are checked for digest, size, block layout and run ownership.

Record fixtures follow the store contract: legal UTF-8 text split into fixed
64 KiB blocks with only the last block short, and an empty body as zero chunks.
Arbitrary (non-UTF-8) bytes are exercised on the codec itself, never smuggled
into a record — production integrity is not relaxed to suit a fixture.

The runtime captures its own safe records for a real run, so every assertion
below is scoped to this fixture's record rather than to table-wide totals.
"""

import base64
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing

import pytest

M2_TABLES = (
    "work_events",
    "work_event_records",
    "work_event_chunks",
    "work_event_sources",
    "work_event_summaries",
    "work_event_audit",
    "work_event_task_links",
    "work_event_reads",
    "event_summary_jobs",
    "event_summary_calls",
)
TASK_TABLES = (
    "tasks",
    "task_checkpoints",
    "task_steps",
    "task_events",
    "task_reviews",
    "dependency_plans",
)
PRIVATE_TABLES = ("chat_run_checkpoints", "chat_tool_effects")
CHUNK_BYTES = 64 * 1024

CHECKPOINT_MARKER = "private-replay-marker-9f1c"
EFFECT_MARKER = "private-effect-marker-4b7a"
RECORD_MARKER = "M2记录正文。"
RECORD_ID = "rec-m2-fixture"
EVENT_ID = "ev-m2-fixture"


def database_tables(path) -> set[str]:
    with closing(sqlite3.connect(str(path))) as db:
        return {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def rows(path, sql: str, parameters: tuple = ()) -> list[tuple]:
    """Typed rows: ``typeof`` keeps BLOB/TEXT storage classes visible after a round trip."""
    with closing(sqlite3.connect(str(path))) as db:
        return [tuple(row) for row in db.execute(sql, parameters)]


def checkpoint_quiet(path) -> None:
    """Fold the WAL so a plain file copy is a complete snapshot."""
    with closing(sqlite3.connect(str(path))) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.commit()


def export_tree_files(directory):
    return sorted(item for item in directory.rglob("*") if item.is_file())


def store_chunks(text: str) -> list[bytes]:
    """Exactly how the store splits one body: 64 KiB blocks, only the last short."""
    data = text.encode("utf-8")
    return [data[index : index + CHUNK_BYTES] for index in range(0, len(data), CHUNK_BYTES)]


def store_digest(chunks) -> str:
    return hashlib.sha256(b"".join(chunks)).hexdigest()


def insert_work_event(app, *, chunks, run_id):
    """Write one event, record and chunk set directly; export fidelity is the subject.

    ``run_id`` is a real completed run: the read ledger references it and the
    record's own agent/conversation must be the ones that run was captured under.
    """
    with app.database.transaction() as db:
        run = db.execute(
            "SELECT agent_id,conversation_id FROM chat_runs WHERE id=?", (run_id,)
        ).fetchone()
        agent_id, conversation_id = run["agent_id"], run["conversation_id"]
        db.execute(
            "INSERT INTO work_events(id,agent_id,conversation_id,title,status,outcome,folded,"
            "revision,request_id,fingerprint,grants,summary,coverage,members,run_ids,"
            "created_at,closed_at) "
            "VALUES(?,?,?,'导出事件','closed','completed',0,2,'req-1','fp-1','{}',"
            "'规则概况','{}','[]',?,'2026-10-17T00:00:00Z','2026-10-17T00:01:00Z')",
            (EVENT_ID, agent_id, conversation_id, json.dumps([run_id])),
        )
        db.execute(
            "INSERT INTO work_event_records(id,run_id,agent_id,conversation_id,kind,step,"
            "call_id,attempt,metadata,grants,sha256,byte_count,created_at) "
            "VALUES(?,?,?,?,'tool_result',1,'call-1',1,NULL,'{}',?,?,"
            "'2026-10-17T00:00:00Z')",
            (
                RECORD_ID,
                run_id,
                agent_id,
                conversation_id,
                store_digest(chunks),
                sum(len(chunk) for chunk in chunks),
            ),
        )
        db.executemany(
            "INSERT INTO work_event_chunks(record_id,ordinal,body) VALUES(?,?,?)",
            [(RECORD_ID, ordinal, body) for ordinal, body in enumerate(chunks)],
        )
        db.execute(
            "INSERT INTO work_event_sources(event_id,source_id,kind,ref,byte_count,sha256,"
            "coverage,ordinal) VALUES(?,?,'record','{}',1,NULL,'retained',0)",
            (EVENT_ID, "src-1"),
        )
        db.execute(
            "INSERT INTO work_event_summaries(event_id,revision,kind,text,created_at) "
            "VALUES(?,2,'rule',?,'2026-10-17T00:01:00Z')",
            (EVENT_ID, RECORD_MARKER),
        )
        db.execute(
            "INSERT INTO work_event_audit(event_id,revision,action,created_at) "
            "VALUES(?,2,'close','2026-10-17T00:01:00Z')",
            (EVENT_ID,),
        )
        db.execute(
            "INSERT INTO work_event_reads(id,run_id,agent_id,conversation_id,event_id,"
            "event_revision,kind,source_id,cursor,next_cursor,sha256,byte_count,page_size,"
            "model_step,envelope_bytes,hits,created_at) VALUES('read-1',?,?,?,?,2,"
            "'record','src-1','','',NULL,7,8192,3,4321,1,'2026-10-17T00:02:00Z')",
            (run_id, agent_id, conversation_id, EVENT_ID),
        )
        db.execute(
            "INSERT INTO event_summary_jobs(id,event_id,agent_id,conversation_id,"
            "event_revision,policy_version,profile_id,max_calls,status,attempts,"
            "cancel_requested,coverage,outcome,error_code,usage,result_revision,created_at,"
            "updated_at,lease_until,lease_token) VALUES('job-1',?,?,?,2,'p4-v1',NULL,4,"
            "'completed',1,0,NULL,'active',NULL,NULL,2,'2026-10-17T00:03:00Z',"
            "'2026-10-17T00:04:00Z',NULL,NULL)",
            (EVENT_ID, agent_id, conversation_id),
        )
        db.execute(
            "INSERT INTO event_summary_calls(job_id,attempt,call_index,stage,input_bytes,"
            "started_at,finished_at,outcome,error_code,usage) VALUES('job-1',1,1,'chunk',"
            "512,'2026-10-17T00:03:00Z','2026-10-17T00:03:30Z','completed',NULL,NULL)"
        )


def completed_run(app, agent_id):
    """One real completed chat turn: the read ledger and ownership need a real run."""
    import asyncio

    import httpx

    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.runtime import ChatRequest, ChatRuntime

    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
    )
    item = app.profiles.save("fixture", profile, api_key="maintenance-secret")
    app.profiles.select(item["id"], None)
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "完成"}, "finish_reason": "stop"}
        ],
    }

    def handler(_request):
        body = "data: " + json.dumps(frame) + "\\n\\ndata: [DONE]\\n\\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(
                ChatRequest(agent_id=agent_id, message="维护夹具", request_id="maintenance-run")
            )
            [event async for event in runtime.events(run["id"])]
            await runtime.aclose()
            return run["id"]

    return asyncio.run(scenario())


@pytest.fixture
def event_run(app, agent_id):
    return completed_run(app, agent_id)


# --- the byte codec itself, independent of any record ----------------------


def test_row_codec_round_trips_arbitrary_bytes_through_base64():
    from lingnexus.maintenance.service import _decode_row, _encode_row

    types = {"body": "BLOB", "text": "TEXT"}
    raw = bytes(range(256)) + b"\\x00\\xfftail"
    encoded = _encode_row({"body": raw, "text": "正文"}, types)
    assert encoded == {"body": {"$b64": base64.b64encode(raw).decode("ascii")}, "text": "正文"}
    assert _decode_row(encoded, types) == {"body": raw, "text": "正文"}
    # A plain string in a BLOB column is left alone: only the marker means bytes.
    assert _decode_row({"body": "", "text": "正文"}, types) == {"body": "", "text": "正文"}
    # The marker is valid only in a declared BLOB column, in exactly that shape.
    marker = {"$b64": "Zm9v"}
    with pytest.raises(ValueError, match="编码"):
        _decode_row({"body": {"$b64": "!not-base64!"}, "text": "x"}, types)
    with pytest.raises(ValueError, match="编码"):
        _decode_row({"body": {"other": "x"}, "text": "x"}, types)
    with pytest.raises(ValueError, match="编码"):
        _decode_row({"body": marker, "text": marker}, {"body": "TEXT", "text": "TEXT"})


# --- version 3: the M2 tables travel, with bytes preserved ----------------


def test_export_v4_declares_continuity_tables_and_a_byte_encoding(app, agent_id, event_run):
    chunks = store_chunks(RECORD_MARKER * 9000)  # More than one 64 KiB block.
    assert len(chunks) > 1 and all(len(chunk) == CHUNK_BYTES for chunk in chunks[:-1])
    assert 0 < len(chunks[-1]) <= CHUNK_BYTES
    insert_work_event(app, chunks=chunks, run_id=event_run)
    destination = app.database.path.parent / "portable"
    manifest = app.maintenance.export_to(destination)

    assert manifest["schema_version"] == 4
    assert manifest["blob_encoding"] == "base64"
    on_disk = {item.stem for item in destination.glob("*.json") if item.name != "manifest.json"}
    assert set(manifest["tables"]) == on_disk
    for table in M2_TABLES:
        assert (destination / f"{table}.json").is_file(), table
    for table in ("continuity_main_conversations", "continuity_experiences", "reflection_jobs"):
        assert (destination / f"{table}.json").is_file(), table
    assert set(manifest["tables"]).isdisjoint(PRIVATE_TABLES)
    # The chunk body is exported as an explicit marker, never as mangled text.
    exported = json.loads((destination / "work_event_chunks.json").read_text(encoding="utf-8"))
    mine = [row for row in exported if row["record_id"] == RECORD_ID]
    assert [row["ordinal"] for row in mine] == list(range(len(chunks)))
    assert [base64.b64decode(row["body"]["$b64"]) for row in mine] == chunks
    reads = json.loads((destination / "work_event_reads.json").read_text(encoding="utf-8"))
    assert [row["event_id"] for row in reads] == [EVENT_ID]
    # The 032 budget metadata columns travel as ordinary integers, not bytes.
    assert [(row["model_step"], row["envelope_bytes"]) for row in reads] == [(3, 4321)]
    jobs = json.loads((destination / "event_summary_jobs.json").read_text(encoding="utf-8"))
    assert [row["status"] for row in jobs] == ["completed"]


def test_record_chunks_round_trip_byte_exact_through_export_and_restore(app, agent_id, event_run):
    text = RECORD_MARKER + "多块正文。" * 20000
    chunks = store_chunks(text)
    assert len(chunks) > 1
    insert_work_event(app, chunks=chunks, run_id=event_run)
    destination = app.database.path.parent / "portable-chunks"
    app.maintenance.export_to(destination)
    restored = app.database.path.parent / "restored-chunks" / "ling.db"
    result = app.maintenance.restore_from(destination, restored)
    assert result["restored"] == str(restored)

    query = (
        "SELECT ordinal,typeof(body),body FROM work_event_chunks WHERE record_id=? "
        "ORDER BY ordinal"
    )
    original = rows(app.database.path, query, (RECORD_ID,))
    copied = rows(restored, query, (RECORD_ID,))
    assert copied == original
    assert [row[1] for row in copied] == ["blob"] * len(chunks)
    assert b"".join(row[2] for row in copied) == text.encode("utf-8")
    summary = "SELECT text FROM work_event_summaries WHERE event_id=? AND revision=2"
    assert rows(restored, summary, (EVENT_ID,)) == [(RECORD_MARKER,)]
    assert rows(restored, "SELECT COUNT(*) FROM work_event_chunks WHERE record_id=?", (RECORD_ID,)) == [
        (len(chunks),)
    ]
    assert rows(restored, "SELECT COUNT(*) FROM event_summary_calls") == [(1,)]
    from lingnexus.maintenance.restore import verify_database

    assert verify_database(restored, restored.parent / "attachments")["attachments"] == 0


def test_empty_record_body_round_trips_as_zero_chunks(app, agent_id, event_run):
    insert_work_event(app, chunks=store_chunks(""), run_id=event_run)
    record = "SELECT sha256,byte_count FROM work_event_records WHERE id=?"
    assert rows(app.database.path, "SELECT COUNT(*) FROM work_event_chunks WHERE record_id=?", (RECORD_ID,)) == [(0,)]
    assert rows(app.database.path, record, (RECORD_ID,)) == [(hashlib.sha256(b"").hexdigest(), 0)]
    destination = app.database.path.parent / "portable-empty"
    app.maintenance.export_to(destination)
    restored = app.database.path.parent / "restored-empty" / "ling.db"
    app.maintenance.restore_from(destination, restored)
    assert rows(restored, "SELECT COUNT(*) FROM work_event_chunks WHERE record_id=?", (RECORD_ID,)) == [(0,)]
    assert rows(restored, record, (RECORD_ID,)) == [(hashlib.sha256(b"").hexdigest(), 0)]


def test_defaulted_metadata_columns_round_trip_through_the_generic_codec(app, agent_id, event_run):
    """032's budget columns are plain integers; the codec must pass them through."""
    insert_work_event(app, chunks=store_chunks("默认列"), run_id=event_run)
    with app.database.transaction() as db:
        conversation = db.execute(
            "SELECT conversation_id FROM chat_runs WHERE id=?", (event_run,)
        ).fetchone()[0]
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO work_event_reads(id,run_id,agent_id,conversation_id,event_id,"
            "event_revision,kind,source_id,cursor,next_cursor,sha256,byte_count,page_size,"
            "hits,created_at) VALUES('read-defaults',?,?,?,?,2,'directory','','c0','c1',"
            "NULL,11,8192,1,'2026-10-17T00:09:00Z')",
            (event_run, agent_id, conversation, EVENT_ID),
        )
    query = "SELECT model_step,envelope_bytes FROM work_event_reads WHERE id=?"
    assert rows(app.database.path, query, ("read-defaults",)) == [(1, 0)]
    destination = app.database.path.parent / "portable-defaults"
    app.maintenance.export_to(destination)
    exported = json.loads((destination / "work_event_reads.json").read_text(encoding="utf-8"))
    defaulted = next(row for row in exported if row["id"] == "read-defaults")
    assert (defaulted["model_step"], defaulted["envelope_bytes"]) == (1, 0)
    restored = app.database.path.parent / "restored-defaults" / "ling.db"
    app.maintenance.restore_from(destination, restored)
    assert rows(restored, query, ("read-defaults",)) == [(1, 0)]


def test_private_checkpoints_and_effects_never_enter_a_portable_export(app, agent_id, event_run):
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO chat_run_checkpoints(run_id,state,updated_at,assistant_message_id) "
            "VALUES(?,?,'2026-10-17T00:05:00Z',NULL) ON CONFLICT(run_id) DO UPDATE "
            "SET state=excluded.state,updated_at=excluded.updated_at",
            (event_run, json.dumps({"marker": CHECKPOINT_MARKER, "payload": {"messages": []}})),
        )
        db.execute(
            "INSERT INTO chat_tool_effects(run_id,step,call_id,status,result) "
            "VALUES(?,1,'excluded-call','completed',?)",
            (event_run, json.dumps([EFFECT_MARKER])),
        )
    insert_work_event(app, chunks=store_chunks(RECORD_MARKER), run_id=event_run)
    destination = app.database.path.parent / "portable-private"
    manifest = app.maintenance.export_to(destination)
    names = {item.name for item in export_tree_files(destination)}
    assert not {"chat_run_checkpoints.json", "chat_tool_effects.json"} & names
    assert set(manifest["tables"]).isdisjoint(PRIVATE_TABLES)
    for file in export_tree_files(destination):
        content = file.read_bytes()
        assert CHECKPOINT_MARKER.encode() not in content, file
        assert EFFECT_MARKER.encode() not in content, file

    restored = app.database.path.parent / "restored-private" / "ling.db"
    app.maintenance.restore_from(destination, restored)
    # The tables exist as schema but hold nothing: an import cannot fabricate them.
    for table in PRIVATE_TABLES:
        assert rows(restored, f"SELECT COUNT(*) FROM {table}") == [(0,)], table


def test_whole_database_backup_keeps_work_events_and_private_replay_material(app, agent_id, event_run):
    chunks = store_chunks("备份正文。")
    insert_work_event(app, chunks=chunks, run_id=event_run)
    checkpoint_quiet(app.database.path)
    backup = app.database.path.parent / "whole.db"
    app.maintenance.backup_to(backup)

    from lingnexus.maintenance.restore import verify_backup

    verify_backup(backup)
    restored = app.database.path.parent / "restored-whole" / "ling.db"
    app.maintenance.restore_backup(backup, restored)
    assert rows(
        restored,
        "SELECT ordinal,body FROM work_event_chunks WHERE record_id=? ORDER BY ordinal",
        (RECORD_ID,),
    ) == [(0, chunks[0])]
    # A file-level snapshot keeps the private tables; that is its documented scope.
    assert rows(restored, "SELECT COUNT(*) FROM chat_run_checkpoints WHERE run_id=?", (event_run,)) == [
        (1,)
    ]
    assert rows(restored, "SELECT COUNT(*) FROM chat_tool_effects WHERE run_id=?", (event_run,)) == [(0,)]


# --- older exports and snapshots stay restorable ---------------------------


def downgrade_export(source, destination, version: int) -> dict:
    """Rebuild a pre-M2 export tree from a v3 tree, as an older release wrote it."""
    destination.mkdir()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    from lingnexus.maintenance.service import V4_TABLES

    dropped = set(M2_TABLES) | set(V4_TABLES)
    if version == 1:
        dropped |= set(TASK_TABLES)
    manifest["tables"] = {
        name: meta for name, meta in manifest["tables"].items() if name not in dropped
    }
    manifest["schema_version"] = version
    manifest.pop("blob_encoding", None)
    if version == 1:
        manifest.pop("task_assets", None)
    for file in export_tree_files(source):
        if file.name == "manifest.json" or file.stem in dropped:
            continue
        target = destination / file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
    if version == 1:
        shutil.rmtree(destination / "task-data", ignore_errors=True)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_export_without_m2_tables_restores(app, agent_id, event_run, version):
    insert_work_event(app, chunks=store_chunks("legacy"), run_id=event_run)
    source = app.database.path.parent / f"portable-v{version}"
    app.maintenance.export_to(source)
    legacy = app.database.path.parent / f"legacy-v{version}"
    manifest = downgrade_export(source, legacy, version)
    expected = {item.stem for item in legacy.glob("*.json") if item.name != "manifest.json"}
    assert set(manifest["tables"]) == expected
    assert set(expected).isdisjoint(M2_TABLES)

    restored = app.database.path.parent / f"restored-v{version}" / "ling.db"
    result = app.maintenance.restore_from(legacy, restored)
    # Migrations recreate the M2 tables empty; the export simply could not carry them.
    assert result["tables"] == len(expected)
    assert set(M2_TABLES).issubset(database_tables(restored))
    for table in M2_TABLES:
        assert rows(restored, f"SELECT COUNT(*) FROM {table}") == [(0,)], table
    from lingnexus.maintenance.restore import verify_database

    verify_database(restored, restored.parent / "attachments", allow_legacy=True)


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_version_table_list_matches_a_pre_m2_release(version):
    from lingnexus.maintenance.service import M2_TABLES as declared_m2
    from lingnexus.maintenance.service import TASK_TABLES as declared_tasks
    from lingnexus.maintenance.service import tables_for_version

    expected = set(tables_for_version(version))
    # A release that predates M2 cannot have written any M2 table.
    assert expected.isdisjoint(declared_m2)
    if version == 1:
        assert expected.isdisjoint(declared_tasks)
        assert expected == set(tables_for_version(3)) - set(declared_m2) - set(declared_tasks)
    else:
        assert set(declared_tasks).issubset(expected)
        assert expected == set(tables_for_version(3)) - set(declared_m2)


def test_legacy_export_that_smuggles_m2_tables_is_rejected(app, agent_id, event_run):
    insert_work_event(app, chunks=store_chunks("smuggle"), run_id=event_run)
    source = app.database.path.parent / "portable-smuggle"
    app.maintenance.export_to(source)
    forged = app.database.path.parent / "legacy-smuggle"
    downgrade_export(source, forged, 2)
    # A v2 release cannot have written this file; claiming it must not import.
    shutil.copy2(source / "work_events.json", forged / "work_events.json")
    manifest = json.loads((forged / "manifest.json").read_text(encoding="utf-8"))
    manifest["tables"]["work_events"] = {"sha256": "0" * 64, "rows": 0}
    (forged / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="未知表"):
        app.maintenance.restore_from(forged, forged / "ling.db")


def test_export_version_and_blob_encoding_must_agree(app, agent_id):
    source = app.database.path.parent / "portable-check"
    manifest = app.maintenance.export_to(source)
    assert manifest["schema_version"] == 4 and manifest["blob_encoding"] == "base64"

    cases = (
        (lambda value: value.update(schema_version=5), "版本"),
        (lambda value: value.pop("blob_encoding"), "编码"),
        (lambda value: value.update(blob_encoding="utf16"), "编码"),
    )
    for index, (mutate, message) in enumerate(cases):
        broken = app.database.path.parent / f"broken-{index}"
        broken.mkdir()
        for file in export_tree_files(source):
            target = broken / file.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
        value = json.loads((broken / "manifest.json").read_text(encoding="utf-8"))
        mutate(value)
        (broken / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError) as caught:
            app.maintenance.restore_from(broken, broken / "ling.db")
        assert message in str(caught.value)


@pytest.mark.parametrize("drop_ledger", [False, True])
def test_whole_snapshot_verification_follows_the_migration_ledger(app, agent_id, drop_ledger):
    from lingnexus.maintenance.restore import verify_database

    checkpoint_quiet(app.database.path)
    copy = app.database.path.parent / f"snapshot-{drop_ledger}.db"
    shutil.copy2(app.database.path, copy)
    with closing(sqlite3.connect(str(copy))) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for table in M2_TABLES:
            db.execute(f'DROP TABLE IF EXISTS "{table}"')
        if drop_ledger:
            db.execute("DELETE FROM schema_migrations WHERE version LIKE '03%'")
        db.commit()

    if drop_ledger:
        # A snapshot that predates the M2 migration keeps verifying unchanged.
        summary = verify_database(copy, copy.parent / "attachments", allow_legacy=True)
        assert summary["agents"] >= 1
    else:
        # A ledger claiming the migration without its tables is never repaired silently.
        with pytest.raises(ValueError, match="完整的 LingNexus 资料库"):
            verify_database(copy, copy.parent / "attachments", allow_legacy=True)


# --- chunk bytes, digest, block layout and run ownership -------------------


def test_verification_rejects_a_chunk_body_that_no_longer_matches_its_digest(app, agent_id, event_run):
    from lingnexus.maintenance.service import verify_work_events

    insert_work_event(app, chunks=store_chunks("摘要约束"), run_id=event_run)
    with app.database.transaction() as db:
        verify_work_events(db)
        # Same length, different bytes: only reassembly can notice.
        db.execute(
            "UPDATE work_event_chunks SET body=? WHERE record_id=? AND ordinal=0",
            ("摘要约束".encode()[:3] + b"X", RECORD_ID),
        )
    with pytest.raises(ValueError, match="摘要"), app.database.transaction() as db:
        verify_work_events(db)


def test_verification_rejects_a_missing_chunk(app, agent_id, event_run):
    from lingnexus.maintenance.service import verify_work_events

    insert_work_event(app, chunks=store_chunks("缺块" * 40), run_id=event_run)
    with app.database.transaction() as db:
        db.execute("DELETE FROM work_event_chunks WHERE record_id=? AND ordinal=0", (RECORD_ID,))
    with pytest.raises(ValueError, match="摘要|序号"), app.database.transaction() as db:
        verify_work_events(db)


def test_verification_rejects_a_broken_block_layout(app, agent_id, event_run):
    """A hash-correct set can still be unreadable: the reader uses fixed 64 KiB blocks."""
    from lingnexus.maintenance.service import verify_work_events

    body = store_chunks("布局约束。" * 12000)
    assert len(body) > 1 and all(len(chunk) == CHUNK_BYTES for chunk in body[:-1])
    half = len(body[0]) // 2
    layouts = {
        # Two short blocks where the first is not a full block.
        "short_first": [body[0][:half], body[0][half:], *body[1:]],
        # Ordinals must start at 0 and stay contiguous.
        "non_contiguous": body,
        # A block larger than one block size would load whole in a single page read.
        "oversized": [b"".join(body)],
        # An empty block is never written by the store.
        "empty_block": [body[0], b"", *body[1:]],
    }
    insert_work_event(app, chunks=body, run_id=event_run)
    for name, chunks in layouts.items():
        ordinals = list(range(len(chunks)))
        if name == "non_contiguous":
            ordinals = [index + 1 for index in ordinals]
        with app.database.transaction() as db:
            db.execute("DELETE FROM work_event_chunks WHERE record_id=?", (RECORD_ID,))
            db.execute(
                "UPDATE work_event_records SET sha256=?,byte_count=? WHERE id=?",
                (store_digest(chunks), sum(len(chunk) for chunk in chunks), RECORD_ID),
            )
            db.executemany(
                "INSERT INTO work_event_chunks(record_id,ordinal,body) VALUES(?,?,?)",
                [(RECORD_ID, ordinal, body) for ordinal, body in zip(ordinals, chunks, strict=True)],
            )
        with pytest.raises(ValueError, match="块|序号"), app.database.transaction() as db:
            verify_work_events(db)
    # The untouched legal body still passes once restored.
    with app.database.transaction() as db:
        db.execute("DELETE FROM work_event_chunks WHERE record_id=?", (RECORD_ID,))
        db.execute(
            "UPDATE work_event_records SET sha256=?,byte_count=? WHERE id=?",
            (store_digest(body), sum(len(chunk) for chunk in body), RECORD_ID),
        )
        db.executemany(
            "INSERT INTO work_event_chunks(record_id,ordinal,body) VALUES(?,?,?)",
            [(RECORD_ID, ordinal, value) for ordinal, value in enumerate(body)],
        )
        verify_work_events(db)


def test_verification_rejects_a_record_re_attributed_to_another_run(app, agent_id, event_run):
    from lingnexus.maintenance.service import verify_work_events

    insert_work_event(app, chunks=store_chunks("归属"), run_id=event_run)
    with app.database.transaction() as db:
        verify_work_events(db)
        # The bytes and digest still match; only the claimed owner changed.
        db.execute("UPDATE work_event_records SET agent_id=? WHERE id=?", ("someone-else", RECORD_ID))
    with pytest.raises(ValueError, match="归属"), app.database.transaction() as db:
        verify_work_events(db)


def test_export_refuses_a_database_whose_chunk_digest_is_already_broken(app, agent_id, event_run):
    insert_work_event(app, chunks=store_chunks("导出前已损坏"), run_id=event_run)
    with app.database.transaction() as db:
        db.execute("DELETE FROM work_event_chunks WHERE record_id=? AND ordinal=0", (RECORD_ID,))
    destination = app.database.path.parent / "portable-broken"
    with pytest.raises(ValueError, match="摘要|序号"):
        app.maintenance.export_to(destination)
    # Nothing was published: the failed export leaves no half-written tree.
    assert not destination.exists()
