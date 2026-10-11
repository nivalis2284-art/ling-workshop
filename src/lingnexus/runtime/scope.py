"""The chat loop's view of one agent's environment for the shared dispatcher.

Dispatch itself lives in one place — ``lingnexus.actions.dispatch`` — shared by
every loop. This module only adapts one chat run to the environment that
dispatcher needs. Chat has no snapshot workspace, so commands are reported as
unavailable and the ``BEFORE_TOOL`` policy hook decides what that means; file
tools run against the owner's authorized directories and every access
re-checks the grants. Browser sessions are not wired into the chat loop yet, so
a browser call is honestly refused rather than silently faked.
"""

from __future__ import annotations

from pathlib import Path

from lingnexus.common.errors import PermissionDenied


class ChatToolScope:
    """Adapts one chat run to the environment the shared dispatcher needs."""

    def __init__(self, app, agent_id: str):
        self.app = app
        self.agent_id = agent_id

    @property
    def workdir(self) -> Path | None:
        """Chat has no authorized command sandbox this wave; commands are unavailable."""
        return None

    @property
    def command_available(self) -> bool:
        return self.workdir is not None

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in self.app.permissions.current(self.agent_id)["readable"])

    def read(self, raw: str) -> tuple[dict, str | None]:
        text = self.app.permissions.read_text(self.agent_id, Path(raw))
        return {"status": "completed", "result": {"text": text}}, None

    def write(self, raw: str, text: str, *, create: bool) -> tuple[dict, str | None]:
        if create:
            self.app.permissions.create_text(self.agent_id, Path(raw), text)
        else:
            self.app.permissions.write_text(self.agent_id, Path(raw), text)
        return {"status": "completed", "result": {"path": raw, "written": True}}, None

    def read_path(self, path: Path) -> str:
        return self.app.permissions.read_text(self.agent_id, path)

    async def run(self, argv: list[str], purpose: str) -> tuple[dict, str | None]:
        raise PermissionDenied("当前对话没有已授权的命令执行环境。")

    async def browser(self, name: str, args: dict) -> tuple[dict, str | None]:
        raise PermissionDenied("当前对话没有可用的浏览器会话。")

    def meta(self, name: str, args: dict) -> tuple[dict, str | None]:
        if name == "request_dependencies":
            from lingnexus.dependencies import DependencyService

            result = {
                "status": "completed",
                "dependency_candidate": DependencyService.propose(**args),
                "dependency_status": "pending_owner",
            }
            return result, None
        raise ValueError("Unknown meta tool")
