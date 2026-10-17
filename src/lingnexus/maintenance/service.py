"""Versioned portable exports. Only known tables are imported, never foreign SQL.

Format versions: 1 (pre task assets), 2 (task assets), 3 (M2 work events).
Version 3 also fixes binary fidelity: a BLOB column is exported as
``{"$b64": "<base64>"}`` and decoded back to bytes on import, so the 64 KiB
record chunks round-trip exactly instead of being silently reinterpreted as
text. Older versions have no BLOB columns and are imported unchanged.

Private replay material (``chat_run_checkpoints``) and the tool-effect ledger
(``chat_tool_effects``) are deliberately absent from every portable format;
they survive only whole-database backups, where the file is copied verbatim.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from lingnexus.maintenance.assets import (
    TASK_TABLES,
    cleanup_created,
    copy_task_assets,
    export_task_assets,
    plain_path,
    read_file,
    require_quiet,
    verify_task_assets,
)
from lingnexus.maintenance.backup import (
    _check_database_destination,
    _staged_database,
    backup,
    restore_backup,
)
from lingnexus.maintenance.blobs import export_blobs, read_verified, safe_blob
from lingnexus.storage.db import Database
from lingnexus.work_events.store import CHUNK_BYTES

# Public portable assets. Runtime credentials must never be added to these tables.
TABLES = (
    "agents",
    "sources",
    "source_revisions",
    "jobs",
    "self_revisions",
    "proposals",
    "proposal_sources",
    "decisions",
    "self_entry_origins",
    "episodes",
    "episode_sources",
    "self_revision_origins",
    "knowledge",
    "knowledge_sources",
    "goals",
    "goal_revisions",
    "goal_sources",
    "model_profiles",
    "settings",
    "attachments",
    "chat_runs",
    "chat_events",
    "source_visibility",
    "conversations",
    "conversation_messages",
    "permission_revisions",
    "actions",
    "learning_job_state",
    "learning_attempts",
    "skills",
    "skill_revisions",
    "chat_conversation_failover",
    "chat_run_profiles",
    "chat_call_attempts",
    "activity_web_grants",
    "activity_scopes",
    "activities",
    "activity_evidence",
    "activity_notifications",
    "activity_events",
    "activity_model_calls",
    "browser_origin_revisions",
    "browser_actions",
    "browser_approvals",
    "tasks",
    "task_checkpoints",
    "task_steps",
    "task_events",
    "task_reviews",
    "conversation_attachments",
    "conversation_segments",
    "memory_edits",
    "dependency_plans",
    # M2 work-event index, safe record chunks, read ledger and summary jobs.
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
"""Tables added by migrations 031-033; absent from older exports and databases."""

BLOB_ENCODING = "base64"
BLOB_MARKER = "$b64"
EXPORT_VERSION = 3
"""Current portable format: task assets plus M2 work events with base64 BLOBs."""


def _column_types(db, table: str) -> dict[str, str]:
    """Declared SQLite types for one bundled table name."""
    return {row[1]: (row[2] or "").upper() for row in db.execute(f'PRAGMA table_info("{table}")')}


def _table_names(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _encode_row(row: dict, types: dict[str, str]) -> dict:
    """JSON-safe row: bytes in a BLOB column become an explicit base64 marker."""
    encoded = {}
    for name, value in row.items():
        if "BLOB" in types.get(name, "") and isinstance(value, (bytes, bytearray, memoryview)):
            encoded[name] = {BLOB_MARKER: base64.b64encode(bytes(value)).decode("ascii")}
        else:
            encoded[name] = value
    return encoded


def _decode_row(row: dict, types: dict[str, str]) -> dict:
    """Restore one exported row; the binary marker is only valid for a BLOB column.

    A plain string in a BLOB column stays a string: the marker, never a guess,
    is what distinguishes stored bytes from stored text.
    """
    decoded = {}
    for name, value in row.items():
        if isinstance(value, dict):
            if (
                "BLOB" not in types.get(name, "")
                or set(value) != {BLOB_MARKER}
                or not isinstance(value[BLOB_MARKER], str)
            ):
                raise ValueError("无效的二进制正文编码")
            try:
                decoded[name] = base64.b64decode(value[BLOB_MARKER], validate=True)
            except ValueError as error:
                raise ValueError("无效的二进制正文编码") from error
        else:
            decoded[name] = value
    return decoded


M2_MIGRATION_TABLES = {
    "031_work_events.sql": (
        "work_events",
        "work_event_records",
        "work_event_chunks",
        "work_event_sources",
        "work_event_summaries",
        "work_event_audit",
        "work_event_task_links",
    ),
    "032_event_reads.sql": ("work_event_reads",),
    "033_event_summaries.sql": ("event_summary_jobs", "event_summary_calls"),
}


def tables_for_version(version: int) -> tuple[str, ...]:
    """The exact table list one portable format version carries."""
    if version == 1:
        # A genuine v1 export predates both task assets and M2 work events.
        return tuple(
            name for name in TABLES if name not in TASK_TABLES and name not in M2_TABLES
        )
    if version == 2:
        return tuple(name for name in TABLES if name not in M2_TABLES)
    if version == 3:
        return TABLES
    raise ValueError("不支持的导出格式或版本")


def required_tables(db) -> set[str]:
    """Tables this database must contain, read from its own migration ledger.

    Each bundled migration commits its tables and its ledger row together, so a
    recorded M2 migration means its tables are required; an older ledger means a
    database (or backup) predating M2 stays valid without them and is migrated on
    open. Task tables are excluded here and only demanded by whole-database checks.
    """
    migrated = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
    required = set(TABLES) - set(M2_TABLES)
    for migration, names in M2_MIGRATION_TABLES.items():
        if migration in migrated:
            required.update(names)
    return required


def verify_work_events(db) -> None:
    """Reassemble every safe record body and check its digest, size and ownership.

    A record's body only exists as the concatenation of its byte chunks, so a
    per-row check cannot see a truncated, reordered or re-typed chunk set. This
    walks the chunks in ordinal order, hashes the stream and compares it with the
    record's own ``sha256``/``byte_count``. The reader addresses chunks by fixed
    64 KiB blocks (``offset // CHUNK_BYTES``), so the ordinal sequence must be
    contiguous from 0, every chunk is at most 64 KiB and only the last may be
    short — a set that hashes correctly but breaks that layout would make a page
    read silently skip or over-read bytes. An empty body is zero chunks with the
    digest of the empty byte string.

    Ownership is then checked against the run the record claims: agent and
    conversation must be the ones that run was captured under, so a chunk set
    cannot be re-attributed to another individual or conversation by editing the
    record row. A database predating 031 has none of these tables and needs no
    check.
    """
    tables = _table_names(db)
    if not {"work_event_records", "work_event_chunks"}.issubset(tables):
        return
    expected = {
        row[0]: (row[1], row[2])
        for row in db.execute("SELECT id,sha256,byte_count FROM work_event_records")
    }
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    current: str | None = None
    digest = hashlib.sha256()
    expected_ordinal = 0
    previous_size = 0
    for record_id, ordinal, body in db.execute(
        "SELECT record_id,ordinal,body FROM work_event_chunks ORDER BY record_id,ordinal"
    ):
        if record_id != current:
            if current is not None:
                digests[current] = digest.hexdigest()
            current, digest = record_id, hashlib.sha256()
            expected_ordinal, previous_size = 0, 0
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise ValueError("安全记录正文不是字节块")  # noqa: TRY004 - persisted row type
        raw = bytes(body)
        digest.update(raw)
        sizes[record_id] = sizes.get(record_id, 0) + len(raw)
        if ordinal != expected_ordinal:
            raise ValueError("安全记录正文块序号不连续")
        # Only the final chunk may be short; the reader addresses fixed blocks.
        if expected_ordinal and previous_size != CHUNK_BYTES:
            raise ValueError("安全记录正文块不足整块")
        if not 0 < len(raw) <= CHUNK_BYTES:
            raise ValueError("安全记录正文块大小无效")
        previous_size = len(raw)
        expected_ordinal += 1
    if current is not None:
        digests[current] = digest.hexdigest()
    # A record with no chunks is an empty body, whose digest is the empty hash.
    empty = hashlib.sha256(b"").hexdigest()
    for record_id, (sha256, byte_count) in expected.items():
        if record_id not in sizes:
            digests.setdefault(record_id, empty)
    for record_id, (sha256, byte_count) in expected.items():
        if digests.get(record_id) != sha256 or sizes.get(record_id, 0) != byte_count:
            raise ValueError("安全记录正文与其摘要不匹配")
    if set(digests) - set(expected):
        raise ValueError("安全记录正文缺少归属记录")
    if db.execute(
        "SELECT 1 FROM work_event_records r JOIN chat_runs c ON c.id=r.run_id "
        "WHERE r.run_id IS NOT NULL AND (c.agent_id<>r.agent_id "
        "OR COALESCE(c.conversation_id,'')<>r.conversation_id) LIMIT 1"
    ).fetchone():
        raise ValueError("安全记录的运行归属不匹配")


class MaintenanceService:
    def __init__(self, database: Database):
        self.database = database

    def export_to(self, destination: Path) -> dict:
        destination = plain_path(destination)
        # Export paths are owner-only operations, never tools exposed to a model.
        manifest = {
            "format": "lingnexus",
            "schema_version": EXPORT_VERSION,
            "blob_encoding": BLOB_ENCODING,
            "tables": {},
        }
        with self.database.transaction() as db:
            require_quiet(db)
            # Verify before creating anything: a corrupt body must not leave a
            # half-written export directory behind.
            verify_work_events(db)
            destination.mkdir(parents=True, exist_ok=False)
            for table in TABLES:
                types = _column_types(db, table)
                rows = [
                    _encode_row(dict(row), types)
                    for row in db.execute(f'SELECT * FROM "{table}"')
                ]
                content = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
                (destination / f"{table}.json").write_bytes(content)
                manifest["tables"][table] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "rows": len(rows),
                }
            export_blobs(
                self.database,
                destination,
                [dict(row) for row in db.execute("SELECT * FROM attachments")],
            )
            manifest["task_assets"] = export_task_assets(
                db, self.database.path.parent / "task-data", destination / "task-data"
            )
        # A missing manifest indicates interrupted/incomplete export.
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    def restore_from(self, source: Path, destination_db: Path) -> dict:
        source, destination_db = plain_path(source), plain_path(destination_db)
        manifest = json.loads(plain_path(source / "manifest.json").read_text(encoding="utf-8"))
        if (
            manifest.get("format") != "lingnexus"
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] not in {1, 2, EXPORT_VERSION}
        ):
            raise ValueError("不支持的导出格式或版本")
        version = manifest["schema_version"]
        if version == EXPORT_VERSION:
            if manifest.get("blob_encoding") != BLOB_ENCODING:
                raise ValueError("不支持的二进制正文编码")
        elif "blob_encoding" in manifest:
            raise ValueError("不支持的二进制正文编码")
        tables = set(manifest.get("tables", {}))
        expected = tables_for_version(version)
        if tables != set(expected):
            raise ValueError("导出清单缺少资料或包含未知表")
        contents: dict[str, list[dict]] = {}
        for table in expected:
            data = read_file(source / f"{table}.json")
            meta = manifest["tables"][table]
            if hashlib.sha256(data).hexdigest() != meta["sha256"]:
                raise ValueError("导出资料校验失败")
            rows = json.loads(data)
            if (
                not isinstance(rows, list)
                or len(rows) != meta["rows"]
                or not all(isinstance(row, dict) for row in rows)
            ):
                raise ValueError("无效的表资料")
            contents[table] = rows
        for row in contents["attachments"]:
            read_verified(source / "attachments", row["blob_name"], row["sha256"])
        tasks = verify_task_assets(
            source / "task-data",
            manifest.get("task_assets"),
            contents.get("tasks", []),
            contents.get("task_checkpoints", []),
            legacy=version == 1,
        )
        success = False
        created_blobs: list = []
        try:
            with _staged_database(destination_db) as staged:
                database = Database(staged)
                with database.transaction() as db:
                    db.execute("PRAGMA defer_foreign_keys=ON")
                    for table in expected:
                        rows = contents[table]
                        types = _column_types(db, table)
                        columns = list(types)
                        names = ",".join(f'"{col}"' for col in columns)
                        placeholders = ",".join("?" for _ in columns)
                        sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
                        for row in rows:
                            if set(row) != set(columns):
                                raise ValueError("数据字段不匹配")
                            decoded = _decode_row(row, types)
                            db.execute(sql, [decoded[col] for col in columns])
                    if db.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("导出资料存在断开的来源引用")
                    # Chunk reassembly, digest and run ownership are not visible to
                    # SQLite's own checks: only the concatenated bytes prove them.
                    verify_work_events(db)
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("恢复后的数据完整性检查失败")
                    db.execute("INSERT INTO episode_search SELECT id,title,summary FROM episodes")
                _check_database_destination(destination_db)
                blob_dir = plain_path(destination_db.parent / "attachments")
                blob_dir.mkdir(exist_ok=True)
                for row in contents["attachments"]:
                    data = read_verified(source / "attachments", row["blob_name"], row["sha256"])
                    target = safe_blob(blob_dir, row["blob_name"])
                    if target.exists():
                        read_verified(blob_dir, row["blob_name"], row["sha256"])
                    else:
                        with target.open("xb") as file:
                            created_blobs.append((target, os.fstat(file.fileno())))
                            file.write(data)
                copy_task_assets(
                    source / "task-data", destination_db.parent / "task-data", tasks, created_blobs
                )
            success = True
            return {"restored": str(destination_db), "tables": len(contents)}
        finally:
            if not success:
                cleanup_created(created_blobs)

    def backup_to(self, destination: Path, *, offline: bool = False) -> None:
        # Only the locked desktop launcher may bypass stale task statuses.
        backup(self.database, destination, offline=offline)

    def restore_backup(self, source: Path, destination: Path) -> dict:
        return restore_backup(source, destination)
