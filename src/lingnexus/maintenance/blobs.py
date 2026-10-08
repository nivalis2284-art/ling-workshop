"""Attachment copies tied to a fixed database snapshot."""

import hashlib
from pathlib import Path

from lingnexus.maintenance.assets import plain_path, read_file


def safe_blob(root: Path, name: str) -> Path:
    if not name or any(c in name for c in "/\\:\x00") or name in {".", ".."}:
        raise ValueError("附件清单名称无效")
    root = plain_path(root)
    path = plain_path(root / name)
    if path.parent != root:
        raise ValueError("附件清单路径无效")
    return path


def read_verified(root: Path, name: str, digest: str) -> bytes:
    data = read_file(safe_blob(root, name))
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("附件校验失败")
    return data


def export_blobs(database, destination: Path, rows: list[dict]) -> None:
    directory = plain_path(destination / "attachments")
    directory.mkdir(exist_ok=False)
    for row in rows:
        data = read_verified(database.path.parent / "attachments", row["blob_name"], row["sha256"])
        safe_blob(directory, row["blob_name"]).write_bytes(data)
