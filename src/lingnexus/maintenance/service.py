"""Versioned portable exports. Only known tables are imported, never foreign SQL."""

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
)


class MaintenanceService:
    def __init__(self, database: Database):
        self.database = database

    def export_to(self, destination: Path) -> dict:
        destination = plain_path(destination)
        # Export paths are owner-only operations, never tools exposed to a model.
        manifest = {"format": "lingnexus", "schema_version": 2, "tables": {}}
        with self.database.transaction() as db:
            require_quiet(db)
            destination.mkdir(parents=True, exist_ok=False)
            for table in TABLES:
                rows = [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
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
            or manifest["schema_version"] not in {1, 2}
        ):
            raise ValueError("不支持的导出格式或版本")
        tables = set(manifest.get("tables", {}))
        required = set(TABLES) - TASK_TABLES if manifest["schema_version"] == 1 else set(TABLES)
        if not required.issubset(tables) or not tables.issubset(TABLES):
            raise ValueError("导出清单缺少资料或包含未知表")
        contents: dict[str, list[dict]] = {}
        for table in TABLES:
            if table not in tables:
                continue
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
            legacy=manifest["schema_version"] == 1,
        )
        success = False
        created_blobs: list = []
        try:
            with _staged_database(destination_db) as staged:
                database = Database(staged)
                with database.transaction() as db:
                    db.execute("PRAGMA defer_foreign_keys=ON")
                    for table, rows in contents.items():
                        columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                        names = ",".join(f'"{col}"' for col in columns)
                        placeholders = ",".join("?" for _ in columns)
                        sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
                        for row in rows:
                            if set(row) != set(columns):
                                raise ValueError("数据字段不匹配")
                            db.execute(sql, [row[col] for col in columns])
                    if db.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("导出资料存在断开的来源引用")
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
