"""One dispatch point for every declared tool.

A loop only needs three steps: read declarations from the registry, call
``ToolDispatcher.execute`` once per tool call, and use the returned
``ToolResult``. Argument validation, path authorization, sandbox routing and
result shaping all happen here; the ``ToolScope`` only says how to reach the
caller's environment (snapshot, authorized directories, sandbox, browser).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from lingnexus.common.errors import DomainError, PermissionDenied
from lingnexus.tools._paths import FileAccessDenied, reject_link

from .catalog import registry as default_registry
from .registry import ToolRegistry, ToolResult

_EXCLUDED = {".git", "venv", ".venv", "node_modules", "__pycache__", ".tmp", ".deps", ".home"}
_SEARCH_SCAN_LIMIT = 5000
_ARGUMENT_BYTES = 65_536
_ARGUMENT_CHARS = 16_384

_STATUS = {
    "completed": "completed",
    "success": "completed",
    "prepared": "blocked",
    "unknown": "unknown",
    "failed": "failed",
    "timeout": "failed",
}


@runtime_checkable
class ToolScope(Protocol):
    """How the dispatcher reaches one caller's environment."""

    agent_id: str
    roots: tuple[Path, ...]  # directories that may be searched
    workdir: Path | None  # sandbox working copy; None means commands are unavailable

    def read(self, raw: str) -> tuple[dict, str | None]: ...

    def write(self, raw: str, text: str, *, create: bool) -> tuple[dict, str | None]: ...

    def read_path(self, path: Path) -> str: ...

    async def run(self, argv: list[str], purpose: str) -> tuple[dict, str | None]: ...

    async def browser(self, name: str, args: dict) -> tuple[dict, str | None]: ...

    def meta(self, name: str, args: dict) -> tuple[dict, str | None]: ...


class ToolDispatcher:
    """Routes one validated tool call to the scope and shapes the result."""

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry or default_registry

    async def execute(self, name: str, arguments: dict, scope: ToolScope) -> ToolResult:
        spec = self.registry.get(name)
        if spec is None:
            return self._fail("invalid_tool_request")
        try:
            args = spec.parameters.model_validate(arguments).model_dump()
        except (ValidationError, ValueError, TypeError):
            return self._fail("invalid_tool_request")
        try:
            if spec.kind == "command":
                return await self._command(name, args, scope)
            if spec.kind == "file":
                return await self._file(name, args, scope)
            if spec.kind == "browser":
                return self._wrap(*(await scope.browser(name, args)))
            if spec.kind == "meta":
                return self._wrap(*scope.meta(name, args))
            return self._fail("invalid_tool_request")
        except (PermissionDenied, FileAccessDenied):
            return self._fail("permission_denied")
        except (ValueError, DomainError, UnicodeError):
            return self._fail("invalid_tool_request")
        except OSError:
            # A failed write can have partial external effects; never silently retry.
            if name in {"create_file", "write_file", "edit_file", "run_command"}:
                return self._fail("tool_operation_failed", status="unknown")
            return self._fail("tool_operation_failed")

    # --- routing ------------------------------------------------------------

    async def _command(self, name: str, args: dict, scope: ToolScope) -> ToolResult:
        argv = args["argv"]
        if (
            any("\0" in part or len(part) > _ARGUMENT_CHARS for part in argv)
            or sum(map(len, argv)) > _ARGUMENT_BYTES
        ):
            return self._fail("invalid_tool_request")
        if scope.workdir is None:
            return self._fail("permission_denied")
        return self._wrap(*(await scope.run(argv, args["purpose"])))

    async def _file(self, name: str, args: dict, scope: ToolScope) -> ToolResult:
        if name == "read_file":
            return self._wrap(*scope.read(args["path"]))
        if name in {"create_file", "write_file"}:
            return self._wrap(
                *scope.write(args["path"], args["text"], create=name == "create_file")
            )
        if name == "edit_file":
            return self._wrap(*self._edit(args, scope))
        if name == "search":
            return self._wrap(self._search(args, scope), None)
        return self._fail("invalid_tool_request")

    def _edit(self, args: dict, scope: ToolScope) -> tuple[dict, str | None]:
        payload, _ = scope.read(args["path"])
        if payload.get("status") != "completed":
            return payload, None
        text = (payload.get("result") or {}).get("text", "")
        old, new = args["old_text"], args["new_text"]
        count = text.count(old)
        if count == 0:
            return {"status": "failed", "error_code": "text_not_found"}, None
        if count > 1 and not args["replace_all"]:
            return {"status": "failed", "error_code": "text_not_unique"}, None
        updated = text.replace(old, new) if args["replace_all"] else text.replace(old, new, 1)
        written, effect = scope.write(args["path"], updated, create=False)
        return {**written, "replaced": count if args["replace_all"] else 1}, effect

    def _search(self, args: dict, scope: ToolScope) -> dict:
        query = args["query"]
        mode = args["match"]
        if args["regex"]:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as exc:
                raise ValueError("Invalid search pattern") from exc

            def hit(value: str) -> bool:
                return pattern.search(value) is not None
        else:
            lowered = query.lower()

            def hit(value: str) -> bool:
                return lowered in value.lower()
        matches: list[dict] = []
        truncated = False
        scanned = 0
        for root in scope.roots:
            if truncated or not root.exists():
                continue
            for path in self._walk(root):
                scanned += 1
                if scanned > _SEARCH_SCAN_LIMIT:
                    truncated = True
                    break
                relative = path.relative_to(root).as_posix()
                if mode in ("name", "both") and hit(relative):
                    matches.append(
                        {"path": relative, "kind": "name", "line": None, "preview": relative}
                    )
                if mode in ("content", "both") and len(matches) < args["max_results"]:
                    try:
                        text = scope.read_path(path)
                    except (OSError, UnicodeError, ValueError, PermissionError):
                        continue
                    for number, line in enumerate(text.splitlines(), 1):
                        if hit(line):
                            matches.append(
                                {
                                    "path": relative,
                                    "kind": "content",
                                    "line": number,
                                    "preview": line.strip()[:200],
                                }
                            )
                            break
                if len(matches) >= args["max_results"]:
                    truncated = True
                    break
        return {"status": "completed", "result": {"matches": matches, "truncated": truncated}}

    def _walk(self, root: Path):
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                entries = os.scandir(folder)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    name = entry.name.lower()
                    if name in _EXCLUDED or name.startswith(".env"):
                        continue
                    path = Path(entry.path)
                    try:
                        info = path.lstat()
                        reject_link(info)
                    except (OSError, FileAccessDenied):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not os.path.ismount(entry.path):
                            stack.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        yield path

    # --- shaping ------------------------------------------------------------

    def _wrap(self, payload: dict, effect: str | None) -> ToolResult:
        status = _STATUS.get(str(payload.get("status")), "failed")
        return ToolResult(status, [payload], payload.get("error_code"), effect)

    def _fail(self, error_code: str, *, status: str = "failed") -> ToolResult:
        payload = {"status": status, "error_code": error_code}
        return ToolResult(status, [payload], error_code, None)
