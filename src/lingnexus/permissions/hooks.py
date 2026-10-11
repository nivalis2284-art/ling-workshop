"""Permission policy as a ``BEFORE_TOOL`` hook.

Authorization lives here, not in any loop body. The hook reads only the call
context carried in the payload and the owner's authorization configuration
supplied by an ``Authorization`` provider; it never reads loop-internal state,
so the same hook works for the chat loop and the task loop alike.

``BEFORE_TOOL`` payload contract::

    {
        "name": str,                 # the tool being called
        "arguments": dict,           # the validated tool arguments
        "context": {
            "agent_id": str,         # whose grants apply
            "command_available": bool # whether a command sandbox is authorized
        },
    }

The hook rejects, with a human-readable reason and a ``replacement`` carrying
that reason for the loop to shape the tool result with, when a call would step
outside the owner's grants: file access outside the authorized directories, an
unauthorized browser target, or command execution with no authorized sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from lingnexus.browser.policy import normalize_origin
from lingnexus.runtime.hooks import Handler, HookResult
from lingnexus.tools._paths import FileAccessDenied, local_path

_READ_TOOLS = frozenset({"read_file"})
_WRITE_TOOLS = frozenset({"create_file", "write_file", "edit_file"})
_PAGE_BROWSER_TOOLS = frozenset({"browser_read", "browser_fill", "browser_request_click"})


@runtime_checkable
class Authorization(Protocol):
    """The owner's grants, as the only configuration the hook depends on."""

    def readable(self, agent_id: str) -> tuple[Path, ...]: ...

    def writable(self, agent_id: str) -> tuple[Path, ...]: ...

    def browser_grants(self, agent_id: str) -> dict: ...


class AppAuthorization:
    """Reads grants from the composition root's Permissions and Browser services."""

    def __init__(self, app) -> None:
        self.app = app

    def readable(self, agent_id: str) -> tuple[Path, ...]:
        return tuple(Path(p) for p in self.app.permissions.current(agent_id)["readable"])

    def writable(self, agent_id: str) -> tuple[Path, ...]:
        return tuple(Path(p) for p in self.app.permissions.current(agent_id)["writable"])

    def browser_grants(self, agent_id: str) -> dict:
        browser = getattr(self.app, "browser", None)
        if browser is None:
            return {"revision": 0, "grants": []}
        return browser.current_origins(agent_id)


def _deny(reason: str, name: str) -> HookResult:
    return HookResult.deny(reason, {"blocked": True, "tool": name, "reason": reason})


def permission_hook(authorization: Authorization) -> Handler:
    """Build the ``BEFORE_TOOL`` handler that enforces the owner's grants."""

    async def handler(payload: dict) -> HookResult | None:
        name = payload.get("name")
        arguments = payload.get("arguments") or {}
        context = payload.get("context") or {}
        agent_id = context.get("agent_id")
        if not agent_id:
            return None
        if name in _READ_TOOLS or name in _WRITE_TOOLS:
            return _check_file(name, arguments, agent_id, authorization)
        if name == "search":
            # ``search`` has no single path to authorize: it only ever walks the
            # agent's readable grants (the dispatcher resolves every hit through
            # the same per-file read). So the gate is simply that a readable
            # directory exists — checking it as a path tool would deny it always.
            if not authorization.readable(agent_id):
                return _deny("尚未授权相应的文件访问目录，请先在权限设置中授权。", name)
            return None
        if name == "run_command":
            if not context.get("command_available"):
                return _deny("当前没有已授权的命令执行环境，无法运行命令。", name)
            return None
        if name == "browser_goto" or name in _PAGE_BROWSER_TOOLS:
            return _check_browser(name, arguments, agent_id, authorization)
        return None

    return handler


def _check_file(
    name: str, arguments: dict, agent_id: str, authorization: Authorization
) -> HookResult | None:
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw:
        return _deny("工具调用缺少文件路径。", name)
    try:
        candidate = local_path(raw)
    except FileAccessDenied:
        return _deny("路径不安全或包含不被允许的写法，已拒绝访问。", name)
    write = name in _WRITE_TOOLS
    roots = authorization.writable(agent_id) if write else authorization.readable(agent_id)
    if not roots:
        return _deny("尚未授权相应的文件访问目录，请先在权限设置中授权。", name)
    if not candidate.is_absolute():
        # Relative paths resolve under the first grant, matching the file tools.
        candidate = roots[0] / candidate
    if not any(candidate.is_relative_to(root) for root in roots):
        action = "写入" if write else "读取"
        return _deny(f"该位置不在已授权目录内，已拒绝{action}：{raw}", name)
    return None


def _check_browser(name: str, arguments: dict, agent_id: str, authorization: Authorization) -> HookResult | None:
    state = authorization.browser_grants(agent_id)
    grants = state.get("grants") or []
    if not grants:
        return _deny("尚未授权任何浏览器目标。", name)
    revision = arguments.get("origin_revision")
    if revision is None or revision != state.get("revision"):
        return _deny("浏览器授权已变更，请先查看当前浏览器状态再操作。", name)
    if name == "browser_goto":
        try:
            origin = normalize_origin(arguments.get("url") or "")
        except (TypeError, ValueError):
            return _deny("浏览器目标地址无效，已拒绝。", name)
        if origin not in {grant.get("origin") for grant in grants}:
            return _deny(f"浏览器目标未获授权，已拒绝访问：{origin}", name)
    return None
