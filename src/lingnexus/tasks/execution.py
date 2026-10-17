"""Task tool dispatch and evidence accounting; owner approvals are never tools.

Dispatch itself lives in one place — ``lingnexus.actions.dispatch`` — shared by
every loop. This module only adapts one task step to that dispatcher (the
``TaskToolScope``) and keeps the task-side evidence accounting that decides
whether a delivered task outcome was actually verified.
"""

import json
from pathlib import Path
from typing import Any

from lingnexus.actions.catalog import registry as TOOL_REGISTRY
from lingnexus.actions.dispatch import ToolDispatcher
from lingnexus.common.errors import DomainError, PermissionDenied
from lingnexus.common.execution_leases import ExecutionLeases, result_fence

# Shared evidence policy is deliberately independent of either runtime.
from lingnexus.common.verification import account as record_evidence
from lingnexus.common.verification import successful
from lingnexus.common.workspace_scan import scan as inspect_workspace
from lingnexus.dependencies import DependencyService
from lingnexus.tools import ExecutionLimits

from .browser import BrowserTools
from .store import encode
from .workspace import authorize, make_parents, scan, tools


def account(state, call, result):
    """Compatibility entry point for the shared evidence policy."""
    record_evidence(state, call, result)


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
            text = tools(root, max_bytes=1_048_576).read_text(self.snapshots.file_path(self.task["id"], raw))
            return {"status": "completed", "result": {"text": text}, "effect_id": self.effect}, self.effect
        result = self.app.actions.execute(
            self.agent_id, self._base + ":read", "read_file", {"path": raw}
        )
        result = json.loads(encode(result))
        return result, result.get("id")

    def write(self, raw: str, text: str, *, create: bool,
              expected_sha256: str | None = None) -> tuple[dict, str | None]:
        if self._snapshot:
            root = self.snapshots.path(self.task["id"])
            path = self.snapshots.file_path(self.task["id"], raw)
            access = tools(root)
            if create:
                make_parents(root, path.relative_to(root))
                access.create_text(path, text)
            else:
                access.write_text(path, text, expected_sha256=expected_sha256)
            scan(root)
            payload = {
                "status": "completed",
                "result": {"path": raw, "written": True},
                "effect_id": self.effect,
            }
            return payload, self.effect
        name = "create_file" if create else "write_file"
        result = self.app.actions.execute(
            self.agent_id, self._base + ":write", name,
            {"path": raw, "text": text, "expected_sha256": expected_sha256}
        )
        result = json.loads(encode(result))
        return result, result.get("id")

    def read_path(self, path: Path) -> str:
        if self._snapshot:
            return tools(self.snapshots.path(self.task["id"]), max_bytes=1_048_576).read_text(path)
        return self.app.permissions.read_text(self.agent_id, path)

    async def run(self, argv: list[str], purpose: str, *, long_running=False) -> tuple[dict, str | None]:
        if not self._snapshot:
            raise PermissionDenied("Command requires an authorized snapshot.")
        authorize(self.app, self.agent_id, self.task["workspace"])
        root = self.snapshots.path(self.task["id"])
        inspect_workspace(root)
        # The tier is chosen by the sandbox service; no engine is hard-coded.
        result = await self.app.sandbox.command(
            self.agent_id, self.task["id"], "task", list(argv), purpose, root,
            authorization_root=self.task["workspace"], image=self.task["docker_image"], long_running=long_running)
        # The session runner hashes once after exit with the shared snapshot policy.
        # Do not repeat content scans or retain a second complete byte snapshot here.
        if result.get("version_unavailable"):
            result["artifact_error"] = "unsafe_or_oversized_snapshot"
        result["execution_purpose"] = purpose
        result["verification_passed"] = bool(purpose == "verify" and successful("run_command", result))
        return result, self.effect

    async def browser(self, name: str, args: dict) -> tuple[dict, str | None]:
        result = await self.browser_tools.execute(self.task, self.step, name, args)
        return result, result.get("id") or self.effect

    def meta(self, name: str, args: dict) -> tuple[dict, str | None]:
        if name in {"command_status", "command_cancel"}:
            return self.app.sandbox.observe(args["session_id"], self.agent_id, self.task["id"], "task",
                                            args.get("after", 0), cancel=name == "command_cancel"), self.effect
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
        self.execution_leases = ExecutionLeases(app)

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
        # Legacy live file actions already use the SQL action lease triggers;
        # staged snapshot writes/commands use exactly the same durable lane.
        if name == "run_command" or (task["workspace"] and name in {"create_file", "write_file", "edit_file"}):
            async with self.execution_leases.guard(f"task:{task['agent_id']}:{tid}") as fence:
                outcome = await self.dispatcher.execute(name, args, scope)
                fence.update(result_fence(outcome.data))
        else:
            if actions_path and name in {"create_file", "write_file", "edit_file"}:
                await self.execution_leases.wait_available()
            outcome = await self.dispatcher.execute(name, args, scope)
        result: dict[str, Any] = outcome.data
        # A tool can read a file containing the currently selected model key.
        # Never copy that echo into public task events or event-recall sources;
        # preserve an unknown fence rather than claiming the effect succeeded.
        try:
            credentials = self.app.profiles.resolve(task["profile_id"])[1]
        except DomainError:
            credentials = None
        if credentials and credentials.api_key in encode(result):
            result = {"status": "unknown", "error_code": "credential_in_response"}
        if outcome.effect_id and outcome.effect_id != step["id"]:
            with self.app.database.transaction():
                self.store.effect(step["id"], outcome.effect_id)
        status = (
            "waiting_owner"
            if result.get("status") == "prepared" and result.get("approval_id")
            else "unknown"
            if result.get("status") == "unknown"
            else "completed"
            if successful(name, result) or result.get("status") in {"running", "cancelling"}
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
