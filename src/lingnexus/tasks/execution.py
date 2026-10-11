"""Task tool dispatch and evidence accounting; owner approvals are never tools.

Dispatch itself lives in one place — ``lingnexus.actions.dispatch`` — shared by
every loop. This module only adapts one task step to that dispatcher (the
``TaskToolScope``) and keeps the task-side evidence accounting that decides
whether a delivered task outcome was actually verified.
"""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lingnexus.actions.catalog import registry as TOOL_REGISTRY
from lingnexus.actions.dispatch import ToolDispatcher
from lingnexus.common.errors import PermissionDenied
from lingnexus.dependencies import DependencyService
from lingnexus.tools import ExecutionLimits

from .browser import BrowserTools
from .store import encode
from .workspace import authorize, digest, make_parents, scan, tools

BROWSER_NAMES = frozenset(s.name for s in TOOL_REGISTRY.specs() if s.kind == "browser")


def successful(name, result):
    if name == "run_command":
        return (
            result.get("status") == "success"
            and result.get("exit_code") == 0
            and result.get("cleanup_confirmed")
            and not result.get("timed_out")
            and not result.get("output_truncated")
            and not result.get("artifact_error")
        )
    return result.get("status") == "completed"


def account(state, call, result):
    name, args = call["name"], call["arguments"]
    key = (
        name + ":" + args.get("path", "")
        if name in {"read_file", "create_file", "write_file"}
        else name + encode(args.get("argv", args))
    )
    okay = successful(name, result)
    if okay:
        state["failures"].pop(key, None)
    else:
        state["failures"][key] = result.get("error_code") or result.get("error") or "tool_failed"
    if name in {"create_file", "write_file"} and okay:
        state["verified"] = False
        state["writes"][args["path"]] = digest(args["text"].encode())
        state["checked"].pop(args["path"], None)
    elif name == "read_file" and okay:
        content = result.get("result", {}).get("text", "")
        state["checked"][args["path"]] = digest(content.encode())
        state["verified"] = bool(state["writes"]) and all(
            state["checked"].get(p) == h for p, h in state["writes"].items()
        )
        if not state["writes"]:
            state["verified"] = True
    elif name == "run_command":
        state["command_used"] = True
        state["verified"] = bool(okay and args.get("purpose") == "verify")
    if name == "run_command" or name in {"create_file", "write_file"}:
        state["command_verified"] = state["verified"] if name == "run_command" else False
    if state["command_used"] or (state.get("coding_workspace") and state["writes"]):
        # File reads cannot replace current test evidence.
        state["verified"] = state.get("command_verified", False)
    if name in BROWSER_NAMES and name != "browser_state":
        state["browser_used"] = True
        state["browser_verified"] = bool(
            okay and name == "browser_read" and result.get("verification_passed")
        )
    if state.get("browser_used"):
        command_required = state["command_used"] or (
            state.get("coding_workspace") and state["writes"]
        )
        files_checked = (
            not state["writes"]
            or state.get("coding_workspace")
            or all(state["checked"].get(p) == h for p, h in state["writes"].items())
        )
        state["verified"] = bool(
            state.get("browser_verified")
            and files_checked
            and (not command_required or state.get("command_verified"))
        )


class TaskToolScope:
    """Adapts one task step to the environment the shared dispatcher needs."""

    def __init__(self, app, store, snapshots, task, step, limits, browser):
        self.app, self.store, self.snapshots = app, store, snapshots
        self.task, self.step, self.limits, self.browser_tools = task, step, limits, browser
        self.agent_id = task["agent_id"]
        self.effect = step["id"]
        self._base = f"task:{task['id']}:{task['step_count']}:{step['call_id']}"
        self._snapshot = bool(task["workspace"])

    @property
    def workdir(self) -> Path | None:
        return self.snapshots.path(self.task["id"]) if self._snapshot else None

    @property
    def roots(self) -> tuple[Path, ...]:
        if self._snapshot:
            return (self.snapshots.path(self.task["id"]),)
        return tuple(Path(p) for p in self.app.permissions.current(self.agent_id)["readable"])

    def read(self, raw: str) -> tuple[dict, str | None]:
        if self._snapshot:
            root = self.snapshots.path(self.task["id"])
            text = tools(root).read_text(self.snapshots.file_path(self.task["id"], raw))
            return {"status": "completed", "result": {"text": text}, "effect_id": self.effect}, self.effect
        result = self.app.actions.execute(
            self.agent_id, self._base + ":read", "read_file", {"path": raw}
        )
        result = json.loads(encode(result))
        return result, result.get("id")

    def write(self, raw: str, text: str, *, create: bool) -> tuple[dict, str | None]:
        if self._snapshot:
            root = self.snapshots.path(self.task["id"])
            path = self.snapshots.file_path(self.task["id"], raw)
            access = tools(root)
            if create:
                make_parents(root, path.relative_to(root))
                access.create_text(path, text)
            else:
                access.write_text(path, text)
            path.chmod(0o666)
            scan(root)
            payload = {
                "status": "completed",
                "result": {"path": raw, "written": True},
                "effect_id": self.effect,
            }
            return payload, self.effect
        name = "create_file" if create else "write_file"
        result = self.app.actions.execute(
            self.agent_id, self._base + ":write", name, {"path": raw, "text": text}
        )
        result = json.loads(encode(result))
        return result, result.get("id")

    def read_path(self, path: Path) -> str:
        if self._snapshot:
            return tools(self.snapshots.path(self.task["id"])).read_text(path)
        return self.app.permissions.read_text(self.agent_id, path)

    async def run(self, argv: list[str], purpose: str) -> tuple[dict, str | None]:
        if not self._snapshot:
            raise PermissionDenied("Command requires an authorized snapshot.")
        authorize(self.app, self.agent_id, self.task["workspace"])
        root = self.snapshots.path(self.task["id"])
        scan(root)
        # The tier is chosen by the sandbox service; no engine is hard-coded.
        runner = self.app.sandbox.executor(
            root, image=self.task["docker_image"], writable=True, limits=self.limits
        )
        result = asdict(await asyncio.to_thread(runner.execute, list(argv)))
        try:
            scan(root)
        except (PermissionError, OSError, ValueError):
            # The execution result remains authoritative even when generated
            # artifacts fail validation. Never lose exit/output/cleanup flags.
            result["artifact_error"] = "unsafe_or_oversized_snapshot"
        result["verification_passed"] = bool(purpose == "verify" and successful("run_command", result))
        return result, self.effect

    async def browser(self, name: str, args: dict) -> tuple[dict, str | None]:
        result = await self.browser_tools.execute(self.task, self.step, name, args)
        return result, result.get("id") or self.effect

    def meta(self, name: str, args: dict) -> tuple[dict, str | None]:
        if name == "read_skill":
            # Knowledge only, and no real-world effect: nothing to reconcile later.
            library = getattr(self.app, "skillpacks", None)
            if library is None:
                return {"status": "failed", "error_code": "skillpacks_unavailable"}, None
            return library.read(args["name"], args.get("resource")), None
        if name == "request_dependencies":
            result = {
                "status": "completed",
                "dependency_candidate": DependencyService.propose(**args),
                "dependency_status": "pending_owner",
                "effect_id": self.effect,
            }
            return result, self.effect
        raise ValueError("Unknown meta tool")


class ToolExecutor:
    def __init__(self, app, store, snapshots, limits=None):
        self.app, self.store, self.snapshots = app, store, snapshots
        self.limits = limits or ExecutionLimits()
        self.browser = BrowserTools(app, store)
        self.dispatcher = ToolDispatcher(TOOL_REGISTRY)

    async def execute(self, task, call):
        tid, name, args = task["id"], call["name"], call["arguments"]
        step = self.store.step(tid, task["step_count"], call)
        if step["status"] in {"completed", "failed", "unknown"}:
            return step["result"]
        spec = self.dispatcher.registry.get(name)
        actions_path = spec is not None and spec.kind == "file" and not task["workspace"]
        replay_safe = spec is not None and spec.kind in {"browser", "meta"}
        if step["status"] == "executing" and not (actions_path or replay_safe):
            result = {
                "status": "unknown",
                "error_code": "effect_needs_review",
                "effect_id": step["effect_id"],
            }
            self.store.result(step["id"], result, "unknown")
            return result
        self.store.executing(step["id"])
        self.store.event(
            tid,
            "tool_started",
            {
                "step_id": step["id"],
                "name": name,
                "call_id": call["id"],
                "purpose": step["purpose"],
            },
        )
        scope = TaskToolScope(self.app, self.store, self.snapshots, task, step, self.limits, self.browser)
        outcome = await self.dispatcher.execute(name, args, scope)
        result: dict[str, Any] = outcome.data
        if outcome.effect_id and outcome.effect_id != step["id"]:
            with self.app.database.transaction():
                self.store.effect(step["id"], outcome.effect_id)
        status = (
            "waiting_owner"
            if result.get("status") == "prepared" and result.get("approval_id")
            else "unknown"
            if result.get("status") == "unknown"
            else "completed"
            if successful(name, result)
            else "failed"
        )
        with self.app.database.transaction():
            self.store.result(step["id"], result, status)
            self.store.event(
                tid,
                "tool_result",
                {"step_id": step["id"], "call_id": call["id"], "name": name, "result": result},
            )
        return result
