"""Owner-prepared restores and strict file validation; no live database replacement."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import RLock
from uuid import uuid4

from lingnexus.common.errors import Conflict
from lingnexus.maintenance.assets import (
    MAX_DEPTH,
    MAX_ENTRIES,
    MAX_FILE,
    MAX_TOTAL,
    TASK_TABLES,
    checkpoint_rows,
    inventory,
    plain_path,
    read_file,
    task_rows,
    verify_task_assets,
    verify_task_baselines,
)
from lingnexus.maintenance.blobs import read_verified
from lingnexus.maintenance.service import required_tables, verify_work_events

QUEUE_FILE = "pending-restore.json"
JOURNAL_FILE = "restore-journal.json"
PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")


def checked_tree(path: Path) -> list[Path]:
    path = plain_path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError("维护资料不存在")
    files, pending, count, total = [], [path], 0, 0
    while pending:
        for entry in pending.pop().iterdir():
            count += 1
            if count > MAX_ENTRIES or len(entry.relative_to(path).parts) > MAX_DEPTH:
                raise ValueError("维护资料数量或路径深度超过限制")
            plain_path(entry)
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                size = entry.lstat().st_size
                total += size
                if size > MAX_FILE or total > MAX_TOTAL:
                    raise ValueError("维护资料大小超过限制")
                files.append(entry)
            else:
                raise ValueError("维护资料包含不支持的文件")
    return sorted(files)


def fingerprint(path: Path) -> str:
    path = plain_path(path)
    digest = hashlib.sha256()
    for file in checked_tree(path):
        name = file.relative_to(path).as_posix() if path.is_dir() else "file"
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(read_file(file)).digest())
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(plain_path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("维护状态格式无效")  # noqa: TRY004 - invalid persisted format
    return value


def write_json(path: Path, value: dict, *, exclusive: bool = False) -> None:
    path = plain_path(path)
    if exclusive and path.exists():
        raise FileExistsError("维护状态已存在")
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        plain_path(path)
        if exclusive:
            # Atomic no-clobber publication, even if another owner operation races us.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=64)
def maintenance_lock(directory: Path) -> RLock:
    return RLock()


def validate_id(plan_id: str) -> str:
    if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
        raise ValueError("恢复计划编号无效")
    return plan_id


def verify_database(
    path: Path,
    attachments: Path,
    *,
    immutable: bool = True,
    task_data: Path | None = None,
    allow_legacy: bool = False,
) -> dict:
    """Read only: never migrate or checkpoint a submitted or staged database."""
    path, attachments = plain_path(path), plain_path(attachments)
    if attachments.exists():
        checked_tree(attachments)
    if immutable and any(
        plain_path(Path(str(path) + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("暂存或备份数据库含有未确认的附属文件")
    try:
        uri = path.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查失败")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("数据库存在断开的资料引用")
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # Requirements follow this database's own migration ledger, so a
            # snapshot taken before a later release stays valid: its ledger does
            # not claim the newer tables. A ledger that claims a migration whose
            # tables are missing is still rejected, never repaired silently.
            if "schema_migrations" not in tables:
                raise ValueError("数据库不是完整的 LingNexus 资料库")
            required = required_tables(db)
            if allow_legacy:
                required -= TASK_TABLES
            if not required.issubset(tables):
                raise ValueError("数据库不是完整的 LingNexus 资料库")
            rows = [dict(row) for row in db.execute("SELECT * FROM attachments")]
            for row in rows:
                read_verified(attachments, row["blob_name"], row["sha256"])
            # Safe event bodies are the one thing only reassembled bytes can prove.
            verify_work_events(db)
            tasks = task_rows(db)
            root = task_data if task_data is not None else path.parent / "task-data"
            assets = inventory(root, tasks, scratch=True)
            # Live scratch is disposable but still subject to the desktop tree checks.
            verify_task_baselines(assets, checkpoint_rows(db))
            return {
                "agents": db.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
                "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "attachments": len(rows),
                "tasks": len(tasks),
            }
    except sqlite3.DatabaseError as error:
        raise ValueError("数据库格式或引用无效") from error


def verify_backup(path: Path) -> dict:
    path = plain_path(path)
    assets = plain_path(Path(str(path) + ".assets"))
    checked_tree(assets)
    manifest = read_json(assets / "manifest.json")
    if (
        manifest.get("format") != "lingnexus-backup"
        or type(manifest.get("version")) is not int
        or manifest["version"] not in {1, 2}
    ):
        raise ValueError("备份格式无效")
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("database_sha256"):
        raise ValueError("备份数据库校验失败")
    summary = verify_database(
        path,
        assets / "attachments",
        task_data=assets / "task-data",
        allow_legacy=manifest["version"] == 1,
    )
    rows = manifest.get("attachments")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("备份附件清单无效")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        actual = dict(db.execute("SELECT blob_name,sha256 FROM attachments"))
        verify_task_assets(
            assets / "task-data",
            manifest.get("task_assets"),
            task_rows(db),
            checkpoint_rows(db),
            legacy=manifest["version"] == 1,
        )
    if actual != {row.get("blob_name"): row.get("sha256") for row in rows}:
        raise ValueError("备份附件引用不匹配")
    return summary


def source_fingerprint(source_format: str, source: Path) -> str:
    if source_format == "export":
        return fingerprint(source)
    if source_format == "backup":
        return hashlib.sha256(
            (fingerprint(source) + fingerprint(Path(str(source) + ".assets"))).encode()
        ).hexdigest()
    raise ValueError("恢复来源格式无效")


def read_queue(directory: Path) -> dict | None:
    path = plain_path(directory / QUEUE_FILE)
    if not path.exists():
        return None
    value = read_json(path)
    if (
        set(value) != {"format", "version", "plan_id"}
        or value.get("format") != "lingnexus-restore-queue"
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("待恢复队列格式无效")
    validate_id(value["plan_id"])
    return value


def load_plan(directory: Path, plan_id: str) -> tuple[Path, dict]:
    stage = plain_path(directory / "restore-staging" / validate_id(plan_id))
    plan = read_json(stage / "plan.json")
    fields = {
        "format",
        "version",
        "plan_id",
        "source_format",
        "source_path",
        "source_sha256",
        "database_sha256",
        "attachments_sha256",
        "summary",
    }
    if plan.get("version") == 2:
        fields.add("task_data_sha256")
    if (
        set(plan) != fields
        or plan.get("format") != "lingnexus-restore-plan"
        or type(plan.get("version")) is not int
        or plan["version"] not in {1, 2}
        or plan.get("plan_id") != plan_id
        or plan.get("source_format") not in {"backup", "export"}
        or not isinstance(plan.get("source_path"), str)
        or not Path(plan["source_path"]).is_absolute()
    ):
        raise ValueError("恢复计划格式无效")
    if fingerprint(stage / "ling.db") != plan["database_sha256"]:
        raise ValueError("恢复暂存数据库已变化")
    if fingerprint(stage / "attachments") != plan["attachments_sha256"]:
        raise ValueError("恢复暂存附件已变化")
    if plan["version"] == 2 and fingerprint(stage / "task-data") != plan["task_data_sha256"]:
        raise ValueError("恢复暂存任务资产已变化")
    if (
        source_fingerprint(plan["source_format"], Path(plan["source_path"]))
        != plan["source_sha256"]
    ):
        raise ValueError("恢复来源已变化，请重新预览")
    summary = verify_database(stage / "ling.db", stage / "attachments", allow_legacy=True)
    if plan["version"] == 1:
        if summary["tasks"]:
            raise ValueError("旧版恢复计划未保护任务资产，请重新预览")
        summary.pop("tasks")
    if summary != plan["summary"]:
        raise ValueError("恢复预览不匹配")
    return stage, plan


class RestoreQueue:
    def __init__(self, app):
        self.app = app
        self.directory = plain_path(app.database.path.parent)
        self.lock = maintenance_lock(self.directory)

    def status(self) -> dict:
        with self.lock:
            queue = read_queue(self.directory)
            return (
                {"status": "queued", "plan_id": queue["plan_id"]} if queue else {"status": "idle"}
            )

    def prepare(self, source_format: str, source_path: str) -> dict:
        with self.lock:
            if read_queue(self.directory) or plain_path(self.directory / JOURNAL_FILE).exists():
                raise Conflict("已有恢复正在等待下次启动")
            if not source_path or not Path(source_path).expanduser().is_absolute():
                raise ValueError("请填写完整的恢复来源路径")
            source = plain_path(Path(source_path))
            if source == plain_path(self.app.database.path):
                raise ValueError("不能以正在使用的数据库作为恢复来源")
            digest = source_fingerprint(source_format, source)
            plan_id = uuid4().hex
            stage = plain_path(self.directory / "restore-staging" / plan_id)
            stage.parent.mkdir(exist_ok=True)
            stage.mkdir()
            if source_format == "backup":
                self.app.maintenance.restore_backup(source, stage / "ling.db")
            else:
                self.app.maintenance.restore_from(source, stage / "ling.db")
            (stage / "task-data").mkdir(exist_ok=True)
            summary = verify_database(stage / "ling.db", stage / "attachments", allow_legacy=True)
            if source_fingerprint(source_format, source) != digest:
                raise ValueError("恢复来源在预览过程中发生变化")
            plan = {
                "format": "lingnexus-restore-plan",
                "version": 2,
                "plan_id": plan_id,
                "source_format": source_format,
                "source_path": str(source),
                "source_sha256": digest,
                "database_sha256": fingerprint(stage / "ling.db"),
                "attachments_sha256": fingerprint(stage / "attachments"),
                "task_data_sha256": fingerprint(stage / "task-data"),
                "summary": summary,
            }
            write_json(stage / "plan.json", plan, exclusive=True)
            return {"plan_id": plan_id, "summary": summary, "status": "prepared"}

    def commit(self, plan_id: str, *, confirmed: bool) -> dict:
        with self.lock:
            if confirmed is not True:
                raise ValueError("恢复需要明确确认")
            validate_id(plan_id)
            if read_queue(self.directory) or plain_path(self.directory / JOURNAL_FILE).exists():
                raise Conflict("已有恢复正在等待下次启动")
            load_plan(self.directory, plan_id)
            write_json(
                self.directory / QUEUE_FILE,
                {
                    "format": "lingnexus-restore-queue",
                    "version": 1,
                    "plan_id": plan_id,
                },
                exclusive=True,
            )
            return {"status": "queued", "plan_id": plan_id, "restart_required": True}
