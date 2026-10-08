"""Bounded, no-follow task assets tied to the tasks/checkpoints in a DB snapshot.

Dependencies (.deps, node_modules, virtualenvs and .home) are continuation data.
Only workspace/.tmp contents are disposable; its directory is preserved.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import UUID

from lingnexus.tools._paths import local_path, open_file, regular_file, reject_link

MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_TASKS = 4096
MAX_DEPTH = 64
EXCLUDED = ["workspace/.tmp"]
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TASK_TABLES = {
    "tasks",
    "task_checkpoints",
    "task_steps",
    "task_events",
    "task_reviews",
    "dependency_plans",
}


def plain_path(path: Path) -> Path:
    path = local_path(Path(path).expanduser().absolute())
    for part in (*reversed(path.parents), path):
        try:
            reject_link(part.lstat())
        except FileNotFoundError:
            continue
        except PermissionError as error:
            raise ValueError("维护路径不能包含链接或重解析点") from error
    return path


def read_file(path: Path) -> bytes:
    path = plain_path(path)
    with open_file(path, "read") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_FILE:
            raise ValueError("维护文件超过大小限制")
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("维护文件超过大小限制")
    return data


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def task_rows(db) -> list[dict]:
    if "tasks" not in _tables(db):
        return []
    return [dict(row) for row in db.execute("SELECT * FROM tasks")]


def checkpoint_rows(db) -> list[dict]:
    if "task_checkpoints" not in _tables(db):
        return []
    return [dict(row) for row in db.execute("SELECT * FROM task_checkpoints")]


def require_quiet(db) -> None:
    tables = _tables(db)
    for table, statuses in (
        ("tasks", "'queued','running'"),
        ("task_reviews", "'applying','unknown'"),
        ("dependency_plans", "'running','unknown'"),
    ):
        if (
            table in tables
            and db.execute(f"SELECT 1 FROM {table} WHERE status IN ({statuses}) LIMIT 1").fetchone()
        ):
            raise ValueError("请暂停任务并确认任务写入和依赖安装已停止，再备份或导出")


def _task_ids(rows: list[dict]) -> set[str]:
    if len(rows) > MAX_TASKS:
        raise ValueError("任务数量超过维护限制")
    ids = set()
    for row in rows:
        tid = row.get("id")
        if not isinstance(tid, str):
            raise ValueError("任务编号必须是资料库中的 UUID")  # noqa: TRY004 - invalid persisted ID
        try:
            value = UUID(tid)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("任务编号必须是资料库中的 UUID") from error
        if tid.lower() not in {str(value), value.hex}:
            raise ValueError("任务编号格式无效")
        if row.get("workspace"):
            ids.add(tid)
    return ids


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("任务资产路径无效")
    try:
        path = local_path(value)
    except PermissionError as error:
        raise ValueError("任务资产路径无效") from error
    if (
        len(path.parts) > MAX_DEPTH
        or path.is_absolute()
        or path.as_posix() != value
        or path.parts[0] not in {"workspace", "baseline"}
    ):
        raise ValueError("任务资产路径不在工作区或基线内")
    return value


def inventory(root: Path, rows: list[dict], *, scratch: bool = False) -> dict:
    """Hash every included regular file; limits apply across all tasks together."""
    root = plain_path(root)
    ids = _task_ids(rows)
    result = {"version": 1, "excluded": EXCLUDED.copy(), "tasks": {}}
    if not root.exists():
        if ids:
            raise ValueError("任务资产缺少工作区或基线")
        return result
    if not root.is_dir():
        raise ValueError("任务资产目录无效")
    entries = []
    for entry in root.iterdir():
        if len(entries) >= MAX_TASKS:
            raise ValueError("任务数量超过维护限制")
        entries.append(entry)
    if {p.name for p in entries} != ids:
        raise ValueError("任务资产目录与资料库任务不匹配")
    count, total = 0, 0
    for tid in sorted(ids):
        task = plain_path(root / tid)
        if not task.is_dir():
            raise ValueError("任务资产目录无效")
        meta = {"directories": {}, "files": {}}
        pending = [task]
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as children:
                for child in children:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ValueError("任务资产数量超过维护限制")
                    path = plain_path(Path(child.path))
                    name = _relative(path.relative_to(task).as_posix())
                    info = path.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        if path.is_mount():
                            raise ValueError("任务资产不能包含挂载点")
                        meta["directories"][name] = stat.S_IMODE(info.st_mode) & 0o777
                        if name == "workspace/.tmp":
                            if not scratch and any(path.iterdir()):
                                raise ValueError("备份包含未声明的临时任务资产")
                        else:
                            pending.append(path)
                    else:
                        regular_file(info)
                        total += info.st_size
                        if info.st_size > MAX_FILE or total > MAX_TOTAL:
                            raise ValueError("任务资产大小超过维护限制")
                        data = read_file(path)
                        if len(data) != info.st_size:
                            raise ValueError("任务资产在读取期间变化")
                        meta["files"][name] = {
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size": len(data),
                            "mode": stat.S_IMODE(info.st_mode) & 0o777,
                        }
        if not {"workspace", "baseline"}.issubset(meta["directories"]):
            raise ValueError("任务资产缺少工作区或基线")
        result["tasks"][tid] = meta
    return result


def verify_task_baselines(manifest: dict, checkpoints: list[dict]) -> None:
    states = {row["task_id"]: json.loads(row["state"]) for row in checkpoints}
    for tid, meta in manifest["tasks"].items():
        state = states.get(tid)
        baseline = {
            name[len("baseline/") :]: file["sha256"]
            for name, file in meta["files"].items()
            if name.startswith("baseline/")
        }
        if not isinstance(state, dict) or state.get("baseline") != baseline:
            raise ValueError("任务基线与检查点不匹配")


def verify_task_assets(
    root: Path, manifest, rows: list[dict], checkpoints: list[dict], *, legacy: bool = False
) -> dict:
    if legacy:
        if _task_ids(rows):
            raise ValueError("旧版备份缺少任务资产，无法恢复任务工作区")
        manifest = {"version": 1, "excluded": EXCLUDED.copy(), "tasks": {}}
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "excluded", "tasks"}
        or type(manifest["version"]) is not int
        or manifest["version"] != 1
        or manifest["excluded"] != EXCLUDED
        or not isinstance(manifest["tasks"], dict)
        or set(manifest["tasks"]) != _task_ids(rows)
    ):
        raise ValueError("任务资产清单无效或缺少任务")
    for meta in manifest["tasks"].values():
        if not isinstance(meta, dict) or set(meta) != {"directories", "files"}:
            raise ValueError("任务资产清单无效")
        for key in ("directories", "files"):
            if not isinstance(meta[key], dict) or len(meta[key]) > MAX_ENTRIES:
                raise ValueError("任务资产清单无效")
            for name, value in meta[key].items():
                _relative(name)
                mode = (
                    value
                    if key == "directories"
                    else value.get("mode")
                    if isinstance(value, dict)
                    else None
                )
                if type(mode) is not int or not 0 <= mode <= 0o777:
                    raise ValueError("任务资产权限无效")
                if key == "files" and (
                    set(value) != {"sha256", "size", "mode"}
                    or not isinstance(value["sha256"], str)
                    or not DIGEST.fullmatch(value["sha256"])
                    or type(value["size"]) is not int
                    or not 0 <= value["size"] <= MAX_FILE
                ):
                    raise ValueError("任务资产文件清单无效")
    actual = inventory(root, rows)
    if actual != manifest:
        raise ValueError("任务资产文件或哈希清单不匹配")
    verify_task_baselines(manifest, checkpoints)
    return manifest


def _mkdir(path: Path, created: list, mode=0o777):
    path = plain_path(path)
    path.mkdir()
    created.append((path, path.lstat()))
    path.chmod(mode)


def copy_task_assets(source: Path, destination: Path, manifest: dict, created: list) -> None:
    """Exclusive creation only; callers roll back owned files if DB publication fails."""
    destination = plain_path(destination)
    if not manifest["tasks"]:
        return
    for tid in manifest["tasks"]:
        if plain_path(destination / tid).exists():
            raise FileExistsError("恢复目标已有任务资产")
    if not destination.exists():
        _mkdir(destination, created)
    for tid, meta in manifest["tasks"].items():
        root = destination / tid
        _mkdir(root, created)
        for name in sorted(meta["directories"], key=lambda p: (p.count("/"), p)):
            _mkdir(root / name, created)
        for name, file in meta["files"].items():
            data = read_file(source / tid / name)
            if len(data) != file["size"] or hashlib.sha256(data).hexdigest() != file["sha256"]:
                raise ValueError("任务资产在复制期间变化")
            path = plain_path(root / name)
            with open_file(path, "create") as stream:
                created.append((path, os.fstat(stream.fileno())))
                stream.write(data)
            path.chmod(file["mode"])
        for name, mode in sorted(meta["directories"].items(), reverse=True):
            (root / name).chmod(mode)


def cleanup_created(created: list) -> None:
    for path, owned in reversed(created):
        try:
            plain_path(path)
            if os.path.samestat(path.lstat(), owned):
                if stat.S_ISDIR(owned.st_mode):
                    path.rmdir()
                else:
                    path.chmod(0o600)
                    path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A directory with concurrent, unowned data must survive.
            if not stat.S_ISDIR(owned.st_mode):
                raise


def export_task_assets(db, source: Path, destination: Path) -> dict:
    rows = task_rows(db)
    manifest = inventory(source, rows, scratch=True)
    verify_task_baselines(manifest, checkpoint_rows(db))
    copy_task_assets(source, destination, manifest, [])
    if inventory(source, rows, scratch=True) != manifest:
        raise ValueError("任务资产在备份期间变化")
    verify_task_assets(destination, manifest, rows, checkpoint_rows(db))
    return manifest
