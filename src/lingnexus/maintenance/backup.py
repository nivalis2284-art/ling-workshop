import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from lingnexus.maintenance.assets import (
    checkpoint_rows,
    cleanup_created,
    copy_task_assets,
    export_task_assets,
    plain_path,
    require_quiet,
    task_rows,
    verify_task_assets,
)
from lingnexus.maintenance.blobs import export_blobs, read_verified, safe_blob


def _check_database_destination(destination: Path, *, check_main: bool = True) -> None:
    destination = plain_path(destination)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if not suffix and not check_main:
            continue
        path = Path(str(destination) + suffix)
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        # lstat also rejects dangling symlinks; they are not ours to replace.
        raise FileExistsError(f"数据库目标或其附属文件已存在: {path}")


@contextmanager
def _staged_database(destination: Path) -> Iterator[Path]:
    """Keep SQLite and its sidecars private, then publish by exclusive creation."""
    destination = plain_path(destination)
    _check_database_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".lingnexus-", dir=destination.parent) as directory:
        staged = Path(directory) / "database.db"
        yield staged
        # Callers must close all SQLite connections before leaving the staging block.
        # Their last close checkpoints the private WAL before the main file is copied.
        _check_database_destination(destination)
        owned = None
        try:
            with staged.open("rb") as source, destination.open("xb") as target:
                owned = os.fstat(target.fileno())
                shutil.copyfileobj(source, target)
            _check_database_destination(destination, check_main=False)
        except BaseException:
            # Never delete public sidecars, even if they appeared during publication.
            # A failed copy owns only the main file it exclusively created.
            if owned is not None:
                try:
                    if os.path.samestat(destination.lstat(), owned):
                        destination.unlink()
                except FileNotFoundError:
                    pass
            raise


def backup(database, destination: Path, *, offline: bool = False) -> None:
    destination = plain_path(destination)
    assets = plain_path(Path(str(destination) + ".assets"))
    if assets.exists() or assets.is_symlink():
        raise FileExistsError("备份目标已存在")
    # BEGIN IMMEDIATE prevents paused workers from claiming running/installing
    # while files are copied. Backup reads the committed snapshot separately.
    with _staged_database(destination) as staged, database.transaction() as guard:
        if not offline:
            require_quiet(guard)
        with closing(database.connect()) as source, closing(sqlite3.connect(staged)) as target:
            source.backup(target)
            target.row_factory = sqlite3.Row
            rows = [dict(row) for row in target.execute("SELECT * FROM attachments")]
            _check_database_destination(destination)
            assets.mkdir()
            export_blobs(database, assets, rows)
            tasks = export_task_assets(
                target, database.path.parent / "task-data", assets / "task-data"
            )
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest = {
        "format": "lingnexus-backup",
        "version": 2,
        "database_sha256": digest,
        "attachments": rows,
        "task_assets": tasks,
    }
    # Completion requires the database, attachments and all task assets.
    with (assets / "manifest.json").open("x", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False)


def restore_backup(source: Path, destination: Path) -> dict:
    source, destination = plain_path(source), plain_path(destination)
    assets = plain_path(Path(str(source) + ".assets"))
    manifest = json.loads(plain_path(assets / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("format") != "lingnexus-backup"
        or type(manifest.get("version")) is not int
        or manifest["version"] not in {1, 2}
    ):
        raise ValueError("无效备份格式")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest.get("database_sha256"):
        raise ValueError("数据库备份校验失败")
    for row in manifest["attachments"]:
        read_verified(assets / "attachments", row["blob_name"], row["sha256"])
    created = []
    succeeded = False
    try:
        with _staged_database(destination) as staged:
            staged.write_bytes(data)
            with closing(sqlite3.connect(staged)) as connection:
                connection.row_factory = sqlite3.Row
                if (
                    connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or connection.execute("PRAGMA foreign_key_check").fetchone()
                ):
                    raise ValueError("数据库备份完整性检查失败")
                rows = [dict(row) for row in connection.execute("SELECT * FROM attachments")]
                if {r["blob_name"]: r["sha256"] for r in rows} != {
                    r["blob_name"]: r["sha256"] for r in manifest["attachments"]
                }:
                    raise ValueError("附件清单不匹配")
                tasks = verify_task_assets(
                    assets / "task-data",
                    manifest.get("task_assets"),
                    task_rows(connection),
                    checkpoint_rows(connection),
                    legacy=manifest["version"] == 1,
                )
            _check_database_destination(destination)
            directory = plain_path(destination.parent / "attachments")
            directory.mkdir(exist_ok=True)
            for row in rows:
                target = safe_blob(directory, row["blob_name"])
                content = read_verified(assets / "attachments", row["blob_name"], row["sha256"])
                if target.exists():
                    read_verified(directory, row["blob_name"], row["sha256"])
                else:
                    with target.open("xb") as file:
                        created.append((target, os.fstat(file.fileno())))
                        file.write(content)
            copy_task_assets(assets / "task-data", destination.parent / "task-data", tasks, created)
        succeeded = True
        return {"restored": str(destination), "attachments": len(rows)}
    finally:
        if not succeeded:
            cleanup_created(created)
