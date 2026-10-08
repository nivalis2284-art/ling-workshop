import hashlib
import json
from pathlib import Path

from lingnexus.common.errors import NotFound
from lingnexus.common.types import new_id, utc_now
from lingnexus.content.extract import extract

MAX_FILE_BYTES = 25 * 1024 * 1024


class Attachments:
    def __init__(self, database):
        self.database = database
        self.directory = database.path.parent / "attachments"

    def add(self, agent_id: str, original_name: str, data: bytes) -> dict:
        if (
            not original_name
            or any(c in original_name for c in "/\\:\x00")
            or original_name in {".", ".."}
        ):
            raise ValueError("无效文件名")
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError("文件为空或超过25MB")
        # Read application identity before running document parsers.
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("个体不存在")
        extension = Path(original_name).suffix.lower()
        parsed = extract(extension, data)
        attachment_id = new_id()
        blob_name = attachment_id + extension
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / blob_name
        with target.open("xb") as file:
            file.write(data)
        try:
            with self.database.transaction() as db:
                db.execute(
                    "INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?)",
                    (
                        attachment_id,
                        agent_id,
                        original_name,
                        blob_name,
                        hashlib.sha256(data).hexdigest(),
                        len(data),
                        json.dumps(parsed, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return self.read(attachment_id)

    def read(self, attachment_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            if not row:
                raise NotFound("附件不存在")
            return {
                "id": row["id"],
                "agent_id": row["agent_id"],
                "original_name": row["original_name"],
                "sha256": row["sha256"],
                "byte_count": row["byte_count"],
                "created_at": row["created_at"],
            } | json.loads(row["extraction"])

    def read_bytes(self, attachment_id: str) -> bytes:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT blob_name,sha256 FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
            if not row:
                raise NotFound("附件不存在")
        path = self.directory / row["blob_name"]
        if path.resolve().parent != self.directory.resolve() or path.is_symlink():
            raise ValueError("附件引用路径无效")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("附件内容校验失败")
        return data

    def list(self, agent_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("无效分页")
        with self.database.transaction() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM attachments WHERE agent_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (agent_id, limit, offset),
                )
            ]
        return [self.read(aid) for aid in ids]
