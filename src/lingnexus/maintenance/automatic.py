"""Daily local backups, independent of model calls and paid background work."""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from lingnexus.maintenance.restore import (
    checked_tree,
    maintenance_lock,
    plain_path,
    read_json,
    validate_id,
    verify_backup,
    write_json,
)


class AutomaticBackups:
    def __init__(self, app):
        self.app = app
        self.directory = plain_path(app.database.path.parent)
        self.settings = self.directory / "automatic-backups.json"
        self.lock = maintenance_lock(self.directory)
        with self.lock:
            if not plain_path(self.settings).exists():
                write_json(
                    self.settings,
                    {
                        "version": 1,
                        "owner": uuid4().hex,
                        "enabled": True,
                        "path": str(self.directory / "backups"),
                        "retention": 14,
                        "last_success_day": None,
                        "last_success_at": None,
                        "last_backup": None,
                        "last_error": None,
                    },
                    exclusive=True,
                )

    def _destination(self, value: str) -> Path:
        if (
            not isinstance(value, str)
            or not value.strip()
            or not Path(value).expanduser().is_absolute()
        ):
            raise ValueError("备份目录必须是完整路径")
        path = plain_path(Path(value))
        if path == Path(path.anchor) or self.directory.is_relative_to(path):
            raise ValueError("请选择专用于备份的文件夹")
        for name in ("attachments", "restore-staging"):
            if path.is_relative_to(self.directory / name):
                raise ValueError("不能将备份写入资料附件或恢复暂存目录")
        if path.exists() and not path.is_dir():
            raise ValueError("备份目标不是文件夹")
        return path

    def _read(self) -> dict:
        value = read_json(self.settings)
        if (
            set(value)
            != {
                "version",
                "owner",
                "enabled",
                "path",
                "retention",
                "last_success_day",
                "last_success_at",
                "last_backup",
                "last_error",
            }
            or type(value.get("version")) is not int
            or value["version"] != 1
            or type(value.get("enabled")) is not bool
            or type(value.get("retention")) is not int
            or not 1 <= value["retention"] <= 365
        ):
            raise ValueError("自动备份设置无效")
        validate_id(value["owner"])
        self._destination(value["path"])
        if value["last_success_day"] is not None:
            date.fromisoformat(value["last_success_day"])
        return value

    @staticmethod
    def _public(value: dict) -> dict:
        return {key: item for key, item in value.items() if key not in {"owner", "version"}}

    def status(self) -> dict:
        with self.lock:
            return self._public(self._read())

    def configure(
        self, *, enabled: bool | None = None, path: str | None = None, retention: int | None = None
    ) -> dict:
        with self.lock:
            value = self._read()
            if enabled is not None:
                if type(enabled) is not bool:
                    raise ValueError("自动备份开关无效")
                value["enabled"] = enabled
            if path is not None:
                value["path"] = str(self._destination(path))
            if retention is not None:
                if type(retention) is not int or not 1 <= retention <= 365:
                    raise ValueError("保留份数应为 1 至 365")
                value["retention"] = retention
            write_json(self.settings, value)
            return self._public(value)

    def tick(self, now: datetime | None = None) -> dict:
        # Parent calls only after idle checks. One lock serializes ticks and owner edits.
        with self.lock:
            value = self._read()
            now = now or datetime.now().astimezone()
            if now.tzinfo is None:
                now = now.astimezone()
            day = now.date().isoformat()
            if not value["enabled"] or (value["last_success_day"] or "") >= day:
                return self._public(value) | {"created": False}
            try:
                folder = self._destination(value["path"])
                folder.mkdir(parents=True, exist_ok=True)
                source = folder / f"auto-{value['owner']}-{day}-{uuid4().hex}.db"
                self.app.maintenance.backup_to(source)
                verify_backup(source)
                write_json(
                    Path(str(source) + ".assets") / "automatic.json",
                    {
                        "format": "lingnexus-automatic-backup",
                        "version": 1,
                        "owner": value["owner"],
                        "file": source.name,
                        "created_at": now.astimezone(UTC).isoformat(),
                    },
                    exclusive=True,
                )
            except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
                value["last_error"] = "自动备份未完成；请检查目录权限、磁盘空间和资料完整性。"
                write_json(self.settings, value)
                return self._public(value) | {"created": False}
            value.update(
                last_success_day=day,
                last_success_at=now.astimezone(UTC).isoformat(),
                last_backup=str(source),
                last_error=None,
            )
            write_json(self.settings, value)
            try:
                self._retain(folder, value)
            except (OSError, ValueError):
                value["last_error"] = "备份已完成，但旧备份清理未完成；旧文件已保留。"
                write_json(self.settings, value)
            return self._public(value) | {"created": True}

    def _owned(self, source: Path, value: dict) -> dict:
        folder = self._destination(value["path"])
        if plain_path(source).parent != folder:
            raise ValueError("备份不在指定目录内")
        assets = plain_path(Path(str(source) + ".assets"))
        marker = read_json(assets / "automatic.json")
        if (
            marker.get("format") != "lingnexus-automatic-backup"
            or marker.get("version") != 1
            or marker.get("owner") != value["owner"]
            or marker.get("file") != source.name
        ):
            raise ValueError("不属于自动备份的文件")
        verify_backup(source)
        manifest = read_json(assets / "manifest.json")
        expected = {"manifest.json", "automatic.json"} | {
            "attachments/" + row["blob_name"] for row in manifest["attachments"]
        }
        task_assets = manifest.get("task_assets", {}).get("tasks", {})
        for task_id, assets_info in task_assets.items():
            expected.update("task-data/" + task_id + "/" + name for name in assets_info["files"])
        if {file.relative_to(assets).as_posix() for file in checked_tree(assets)} != expected:
            raise ValueError("备份目录包含其他文件")
        expected_top = {
            "manifest.json",
            "automatic.json",
            "attachments",
        }
        if task_assets:
            expected_top.add("task-data")
        if {entry.name for entry in assets.iterdir()} != expected_top:
            raise ValueError("备份目录包含其他目录")
        if any(entry.is_dir() for entry in (assets / "attachments").iterdir()):
            raise ValueError("附件目录包含其他目录")
        if any(
            plain_path(Path(str(source) + suffix)).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("备份数据库仍被使用")
        datetime.fromisoformat(marker["created_at"])
        return marker

    def _retain(self, folder: Path, value: dict) -> None:
        candidates = []
        for source in folder.glob(f"auto-{value['owner']}-*.db"):
            try:
                marker = self._owned(source, value)
                candidates.append((marker["created_at"], source))
            except (OSError, ValueError, KeyError, TypeError):
                continue  # Corrupt, foreign, linked and incomplete sets are evidence, not garbage.
        candidates.sort()
        for _, source in candidates[: -value["retention"]]:
            self._owned(source, value)  # Recheck immediately before deleting known files only.
            assets = Path(str(source) + ".assets")
            source.unlink()
            for file in checked_tree(assets):
                plain_path(file).unlink()
            directories = []
            for path in assets.rglob("*"):
                safe = plain_path(path)
                if safe.is_dir():
                    directories.append(safe)
            for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                path.rmdir()
            assets.rmdir()
