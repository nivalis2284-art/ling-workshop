import json
from pathlib import Path

from lingnexus.common.errors import NotFound, PermissionDenied, RevisionConflict
from lingnexus.common.types import utc_now
from lingnexus.tools import FileAccessDenied, FilePermissions, WorkspaceTools


class Permissions:
    """Only owner-facing operations configure grants; tools recheck every invocation."""

    def __init__(self, database):
        self.database = database

    def current(self, agent_id: str) -> dict:
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("个体不存在")
            row = db.execute(
                "SELECT * FROM permission_revisions WHERE agent_id=? ORDER BY revision DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            return {
                "agent_id": agent_id,
                "revision": row["revision"] if row else 0,
                "readable": json.loads(row["readable"]) if row else [],
                "writable": json.loads(row["writable"]) if row else [],
            }

    def configure(
        self, agent_id: str, *, readable: list[Path], writable: list[Path], expected_revision: int
    ) -> dict:
        permissions = FilePermissions(tuple(readable), tuple(writable))
        with self.database.transaction() as db:
            state = self.current(agent_id)
            if state["revision"] != expected_revision:
                raise RevisionConflict("授权已变化，请重新查看")
            db.execute(
                "INSERT INTO permission_revisions VALUES (?,?,?,?,?)",
                (
                    agent_id,
                    expected_revision + 1,
                    json.dumps([str(p) for p in permissions.readable_roots]),
                    json.dumps([str(p) for p in permissions.writable_roots]),
                    utc_now(),
                ),
            )
            return self.current(agent_id)

    def _tool(self, agent_id: str, path: Path, *, write: bool = False) -> WorkspaceTools:
        state = self.current(agent_id)
        roots = state["writable"] if write else state["readable"]
        if not roots:
            raise PermissionDenied("尚未授权相应文件访问")
        # Lexical choice is followed by WorkspaceTools' secure traversal and handle checks.
        permissions = FilePermissions(
            tuple(Path(r) for r in state["readable"]), tuple(Path(r) for r in state["writable"])
        )
        return WorkspaceTools(Path(roots[0]), permissions)

    def read_text(self, agent_id: str, path: Path) -> str:
        with self.database.transaction():
            try:
                return self._tool(agent_id, path).read_text(path)
            except FileAccessDenied:
                raise PermissionDenied("读取位置不在授权范围或路径不安全") from None

    def create_text(self, agent_id: str, path: Path, text: str) -> None:
        with self.database.transaction():
            try:
                self._tool(agent_id, path, write=True).create_text(path, text)
            except FileAccessDenied:
                raise PermissionDenied("创建位置不在授权范围或路径不安全") from None

    def write_text(self, agent_id: str, path: Path, text: str,
                   *, expected_sha256: str | None = None) -> None:
        with self.database.transaction():
            try:
                self._tool(agent_id, path, write=True).write_text(
                    path, text, expected_sha256=expected_sha256)
            except FileAccessDenied:
                raise PermissionDenied("修改位置不在授权范围或路径不安全") from None
