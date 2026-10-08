"""Offline restore publication. Call with the desktop instance lock, before any server."""

import hashlib
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from lingnexus.maintenance.restore import (
    JOURNAL_FILE,
    QUEUE_FILE,
    checked_tree,
    fingerprint,
    load_plan,
    maintenance_lock,
    plain_path,
    read_json,
    read_queue,
    validate_id,
    verify_backup,
    verify_database,
    write_json,
)
from lingnexus.maintenance.service import MaintenanceService
from lingnexus.storage.db import Database

LIVE_NAMES = (
    "ling.db",
    "ling.db-wal",
    "ling.db-shm",
    "ling.db-journal",
    "attachments",
    "task-data",
)
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _rollback_backup(directory: Path, plan_id: str) -> Path:
    return plain_path(directory / "backups" / f"before_restore-{validate_id(plan_id)}.db")


def _read_journal(directory: Path) -> tuple[Path, dict]:
    journal = read_json(directory / JOURNAL_FILE)
    new_names = {"ling.db", "attachments"}
    if journal.get("version") == 2:
        new_names.add("task-data")
    if (
        set(journal) != {"format", "version", "plan_id", "phase", "old", "new"}
        or journal.get("format") != "lingnexus-restore-journal"
        or type(journal.get("version")) is not int
        or journal["version"] not in {1, 2}
        or journal.get("phase") not in ("preparing", "applying", "applied", "rolled_back")
        or not isinstance(journal.get("old"), dict)
        or not isinstance(journal.get("new"), dict)
        or not set(journal["old"]).issubset(LIVE_NAMES)
        or "ling.db" not in journal["old"]
        or set(journal["new"]) != new_names
        or any(
            not isinstance(v, str) or not DIGEST.fullmatch(v)
            for v in [*journal["old"].values(), *journal["new"].values()]
        )
    ):
        raise ValueError("恢复日志无效；请保留资料并检查恢复状态")
    stage = plain_path(directory / "restore-staging" / validate_id(journal["plan_id"]))
    for name in ("previous", "incoming", "failed"):
        checked_tree(stage / name)
    for name in LIVE_NAMES:
        plain_path(directory / name)
    if journal["version"] == 1 and (directory / "task-data").exists():
        raise ValueError("旧版恢复日志未保护任务资产，已停止启动")
    return stage, journal


def _archive(directory: Path, stage: Path, journal: dict) -> dict:
    # Archive intent first. If power fails here, the terminal journal makes retry idempotent.
    queued = read_queue(directory)
    if queued is not None:
        if queued["plan_id"] != journal["plan_id"]:
            raise ValueError("恢复队列与日志不匹配")
        target = plain_path(stage / f"{journal['phase']}-intent.json")
        if target.exists():
            raise FileExistsError("恢复意图归档已存在")
        os.replace(directory / QUEUE_FILE, target)
    target = plain_path(stage / f"{journal['phase']}-journal.json")
    if target.exists():
        raise FileExistsError("恢复日志归档已存在")
    os.replace(directory / JOURNAL_FILE, target)
    rollback = _rollback_backup(directory, journal["plan_id"])
    try:
        verify_backup(rollback)
        rollback_path = str(rollback)
    except (ValueError, OSError, KeyError, TypeError):
        rollback_path = None  # A preparation failure may precede a completed rollback backup.
    return {
        "status": journal["phase"],
        "plan_id": journal["plan_id"],
        "rollback_backup": rollback_path,
    }


def _recover(directory: Path) -> dict:
    stage, journal = _read_journal(directory)
    if journal["phase"] == "applied":
        verify_database(directory / "ling.db", directory / "attachments", allow_legacy=True)
        return _archive(directory, stage, journal)
    if journal["phase"] == "rolled_back":
        return _archive(directory, stage, journal)
    if journal["phase"] == "preparing":
        if any((stage / "previous").iterdir()):
            raise ValueError("准备阶段含有未知回滚原件，已停止启动")
        journal["phase"] = "rolled_back"
        write_json(directory / JOURNAL_FILE, journal)
        return _archive(directory, stage, journal)
    # No file is discarded: interrupted incoming data goes to failed/, originals return
    # from previous/. Presence and hashes cover a crash immediately after any os.replace.
    for name in LIVE_NAMES:
        current, previous, failed = (
            directory / name,
            stage / "previous" / name,
            stage / "failed" / name,
        )
        old_digest = journal["old"].get(name)
        new_digest = journal["new"].get(name)
        if previous.exists():
            if old_digest is None or fingerprint(previous) != old_digest:
                raise ValueError("恢复回滚原件校验失败")
            if current.exists():
                if new_digest is None or fingerprint(current) != new_digest or failed.exists():
                    raise ValueError("当前资料在恢复期间变化，已停止回滚以保护文件")
                os.replace(current, failed)
            os.replace(previous, current)
        elif old_digest is not None:
            if not current.exists() or fingerprint(current) != old_digest:
                raise ValueError("无法确认回滚原件，已停止启动")
        elif current.exists():
            if new_digest is None or fingerprint(current) != new_digest or failed.exists():
                raise ValueError("恢复目标出现未知资料，已停止启动")
            os.replace(current, failed)
    journal["phase"] = "rolled_back"
    write_json(directory / JOURNAL_FILE, journal)
    return _archive(directory, stage, journal)


def _copy_incoming(stage: Path) -> Path:
    incoming = plain_path(stage / "incoming")
    shutil.copy2(stage / "ling.db", incoming / "ling.db")
    shutil.copytree(stage / "attachments", incoming / "attachments")
    task_data = plain_path(stage / "task-data")
    if task_data.exists():
        shutil.copytree(task_data, incoming / "task-data")
    else:
        (incoming / "task-data").mkdir()
    for file in checked_tree(incoming):
        with file.open("rb+") as stream:
            os.fsync(stream.fileno())
    verify_database(incoming / "ling.db", incoming / "attachments", allow_legacy=True)
    return incoming


def apply_pending_restore(data_dir: Path) -> dict:
    """Requires exclusive launcher ownership and no open Application/server.

    Invalid queues/sources raise before publication. A failed publication rolls back;
    rollback failures raise and MUST prevent startup. All original history is retained.
    """
    directory = plain_path(Path(data_dir))
    with maintenance_lock(directory):
        if plain_path(directory / JOURNAL_FILE).exists():
            return _recover(directory)
        queue = read_queue(directory)
        if queue is None:
            return {"status": "idle"}
        stage, plan = load_plan(directory, queue["plan_id"])
        for name in LIVE_NAMES:
            path = plain_path(directory / name)
            if path.exists():
                checked_tree(path)
                if (name in {"attachments", "task-data"}) != path.is_dir():
                    raise ValueError("当前数据库或附件路径类型无效")
        current = directory / "ling.db"
        if not current.is_file():
            raise ValueError("当前资料库不存在，不能确认回滚保护")
        verify_database(current, directory / "attachments", immutable=False)
        # Database is used directly; Application is never constructed on the old state.
        # The launcher lock supplies process ownership; SQLite also checks for active writers.
        with closing(sqlite3.connect(current, timeout=0)) as db:
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise ValueError("资料库仍被使用，请完全退出后再恢复")
            db.execute("BEGIN EXCLUSIVE")
            db.rollback()
        for name in ("incoming", "previous", "failed"):
            work = plain_path(stage / name)
            work.mkdir(exist_ok=True)
            if any(work.iterdir()):
                raise ValueError("恢复工作目录已存在未知文件")
        journal = {
            "format": "lingnexus-restore-journal",
            "version": 2,
            "plan_id": plan["plan_id"],
            "phase": "preparing",
            "old": {
                name: fingerprint(directory / name)
                for name in LIVE_NAMES
                if (directory / name).exists()
            },
            "new": {
                "ling.db": plan["database_sha256"],
                "attachments": plan["attachments_sha256"],
                "task-data": plan.get("task_data_sha256", hashlib.sha256().hexdigest()),
            },
        }
        write_json(directory / JOURNAL_FILE, journal, exclusive=True)
        try:
            rollback = _rollback_backup(directory, queue["plan_id"])
            plain_path(Path(str(rollback) + ".assets"))
            rollback.parent.mkdir(exist_ok=True)
            MaintenanceService(Database(current)).backup_to(rollback, offline=True)
            verify_backup(rollback)
            incoming = _copy_incoming(stage)
            load_plan(directory, queue["plan_id"])
            if any(
                fingerprint(incoming / name) != digest for name, digest in journal["new"].items()
            ):
                raise ValueError("恢复复制校验失败")
            journal["old"] = {
                name: fingerprint(directory / name)
                for name in LIVE_NAMES
                if (directory / name).exists()
            }
            journal["phase"] = "applying"
            write_json(directory / JOURNAL_FILE, journal)
            for name in journal["old"]:
                os.replace(directory / name, stage / "previous" / name)
            os.replace(incoming / "attachments", directory / "attachments")
            os.replace(incoming / "task-data", directory / "task-data")
            os.replace(incoming / "ling.db", current)
            verify_database(current, directory / "attachments", allow_legacy=True)
            journal["phase"] = "applied"
            write_json(directory / JOURNAL_FILE, journal)
        except (OSError, ValueError, sqlite3.DatabaseError):
            # BaseException intentionally leaves the durable journal for next startup.
            return _recover(directory)
        return _archive(directory, stage, journal)
