"""Task tool dispatch and evidence accounting; owner approvals are never tools."""

import asyncio
import json
from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lingnexus.actions.catalog import SCHEMAS
from lingnexus.common.errors import DomainError, PermissionDenied
from lingnexus.dependencies import DependencyCandidate, DependencyService
from lingnexus.tools import ExecutionLimits

from .browser import SCHEMAS as BROWSER_SCHEMAS
from .browser import BrowserTools
from .store import encode
from .workspace import authorize, digest, make_parents, scan, tools


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(min_length=1, max_length=100)
    purpose: str


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
    if name in BROWSER_SCHEMAS and name != "browser_state":
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


class ToolExecutor:
    def __init__(self, app, store, snapshots, limits=None):
        self.app, self.store, self.snapshots = app, store, snapshots
        self.limits = limits or ExecutionLimits()
        self.browser = BrowserTools(app, store)

    async def execute(self, task, call):
        tid, name, args = task["id"], call["name"], call["arguments"]
        result: dict[str, Any]
        intent = None
        step = self.store.step(tid, task["step_count"], call)
        if step["status"] in {"completed", "failed", "unknown"}:
            return step["result"]
        request_id = f"task:{tid}:{task['step_count']}:{call['id']}"
        use_actions = name in SCHEMAS and (not task["workspace"] or "path" not in args)
        if (
            step["status"] == "executing"
            and not use_actions
            and name not in BROWSER_SCHEMAS
            and name != "request_dependencies"
        ):
            result = {
                "status": "unknown",
                "error_code": "effect_needs_review",
                "effect_id": step["effect_id"],
            }
            self.store.result(step["id"], result, "unknown")
            return result
        try:
            if (
                name not in SCHEMAS
                and name not in BROWSER_SCHEMAS
                and name not in {"run_command", "request_dependencies"}
            ):
                raise ValueError
            if name in BROWSER_SCHEMAS:
                args = BROWSER_SCHEMAS[name].model_validate(args).model_dump()
            elif name == "run_command":
                validated = Command.model_validate(args)
                if (
                    validated.purpose not in {"work", "verify"}
                    or any("\0" in a or len(a) > 16_384 for a in validated.argv)
                    or sum(map(len, validated.argv)) > 65_536
                ):
                    raise ValueError
            elif name == "request_dependencies":
                args = DependencyCandidate.model_validate(args).model_dump()
            else:
                args = SCHEMAS[name].model_validate(args).model_dump()
            if use_actions:
                intent = self.app.actions.prepare(task["agent_id"], request_id, name, args)
                self.store.executing(step["id"], intent["id"])
            else:
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
            if name in BROWSER_SCHEMAS:
                result = await self.browser.execute(task, step, name, args)
            elif name == "request_dependencies":
                result = {
                    "status": "completed",
                    "dependency_candidate": DependencyService.propose(**args),
                    "dependency_status": "pending_owner",
                    "effect_id": step["id"],
                }
            elif name == "run_command":
                if not task["workspace"]:
                    raise PermissionDenied("Command requires an authorized snapshot.")
                authorize(self.app, task["agent_id"], task["workspace"])
                root = self.snapshots.path(tid)
                scan(root)
                # The tier is chosen by the sandbox service; no engine is hard-coded.
                runner = self.app.sandbox.executor(
                    root, image=task["docker_image"], writable=True, limits=self.limits
                )
                result = asdict(
                    await asyncio.to_thread(runner.execute, list(args["argv"]))
                )
                try:
                    scan(root)
                except (PermissionError, OSError, ValueError):
                    # The execution result remains authoritative even when generated
                    # artifacts fail validation. Never lose exit/output/cleanup flags.
                    result["artifact_error"] = "unsafe_or_oversized_snapshot"
                result["verification_passed"] = bool(
                    args["purpose"] == "verify" and successful(name, result)
                )
            elif use_actions:
                if intent is None:
                    raise ValueError("Action intent was not prepared.")
                result = self.app.actions.execute(task["agent_id"], request_id, name, args)
                # Source references are serialized, never credential objects.
                result = json.loads(encode(result))
            else:
                authorize(self.app, task["agent_id"], task["workspace"])
                root = self.snapshots.path(tid)
                path = self.snapshots.file_path(tid, args["path"])
                access = tools(root)
                if name == "read_file":
                    value = {"text": access.read_text(path)}
                else:
                    if name == "create_file":
                        make_parents(root, path.relative_to(root))
                        access.create_text(path, args["text"])
                    else:
                        access.write_text(path, args["text"])
                    path.chmod(0o666)
                    value = {"path": args["path"], "written": True}
                    scan(root)
                result = {"status": "completed", "result": value, "effect_id": step["id"]}
        except (PermissionDenied, PermissionError):
            result = {"status": "failed", "error_code": "permission_denied"}
        except (ValueError, ValidationError, DomainError, UnicodeError):
            result = {"status": "failed", "error_code": "invalid_tool_request"}
        except OSError:
            result = {
                "status": "unknown"
                if name in {"create_file", "write_file", "run_command"}
                else "failed",
                "error_code": "tool_operation_failed",
            }
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
