"""Task tool dispatch and evidence accounting; owner approvals are never tools."""

import asyncio
import json
from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lingnexus.actions.catalog import SCHEMAS
from lingnexus.common.errors import DomainError, PermissionDenied, SourceInvalid
from lingnexus.common.types import SourceRef
from lingnexus.dependencies import DependencyCandidate, DependencyService
from lingnexus.memory.maintenance import MemoryMaintenance
from lingnexus.tools import DockerExecutor, ExecutionLimits

from .browser import SCHEMAS as BROWSER_SCHEMAS
from .browser import BrowserTools
from .store import encode
from .workspace import authorize, digest, make_parents, scan, tools

MEMORY_TOOLS = {"find_memories", "read_episode", "read_source", "find_knowledge"}


def validate_memory(app, agent_id, state):
    """Call within a short transaction; never adopt current versions for old text."""
    if state.get("memory_changed"):
        raise SourceInvalid("Task memory changed; submit a new task.")
    MemoryMaintenance(app).validate_markers(agent_id, state.get("memory_refs", []))
    refs = state.get("source_refs", [])
    if refs:
        app.history.validate_refs(agent_id, [SourceRef(**r) for r in refs])


def capture_memory(app, agent_id, name, result):
    """Pin only objects returned by Actions, in the transaction that read them."""
    result["memory_refs"], result["source_refs"] = [], []
    if not successful(name, result):
        return
    value = result["result"]
    if name == "read_source":
        result["source_refs"].append(value["source"])
    else:
        kind = "knowledge" if name == "find_knowledge" else "episodes"
        items = [value] if name == "read_episode" else value[kind]
        for item in items:
            expected = {"kind": kind, "id": item["id"], "revision": item["revision"]}
            result["memory_refs"].append(expected)
            result["source_refs"].extend(item["sources"])
        for index, expected in enumerate(result["memory_refs"]):
            marker = MemoryMaintenance(app).snapshot_marker(kind, expected["id"])
            if marker != expected:
                # A recovered completed Action may contain older text. Keep that
                # result, but never label it with a freshly captured revision.
                raise SourceInvalid("Task recalled an older memory revision.")
            result["memory_refs"][index] = marker
    validate_memory(app, agent_id, result)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(min_length=1, max_length=100)
    purpose: str


class SkillRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    skill_id: str = Field(min_length=1, max_length=100)


class SkillSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=32000)


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
    elif (
        okay
        and name in {"find_memories", "read_episode", "read_source", "find_knowledge"}
        and not state["writes"]
    ):
        state["verified"] = True
    if name == "run_command" or name in {"create_file", "write_file"}:
        state["command_verified"] = state["verified"] if name == "run_command" else False
    if state["command_used"] or (state.get("coding_workspace") and state["writes"]):
        # File/memory/skill reads cannot replace current test evidence.
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
        with self.app.database.transaction():
            checkpoint = self.store.load(tid)
            if self.app.identity.current(task["agent_id"]).revision != checkpoint.get(
                "self_revision"
            ):
                return {"status": "blocked", "error_code": "self_changed"}
            try:
                validate_memory(self.app, task["agent_id"], checkpoint)
            except (DomainError, ValueError, TypeError, KeyError):
                return {"status": "blocked", "error_code": "memory_changed"}
        step = self.store.step(tid, task["step_count"], call)
        if step["status"] in {"completed", "failed", "unknown"}:
            if (
                name in MEMORY_TOOLS
                and successful(name, step["result"])
                and ("memory_refs" not in step["result"] or "source_refs" not in step["result"])
            ):
                return {**step["result"], "memory_changed": True}
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
                and name not in {"run_command", "read_skill", "save_skill", "request_dependencies"}
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
            elif name == "read_skill":
                args = SkillRead.model_validate(args).model_dump()
            elif name == "save_skill":
                args = SkillSave.model_validate(args).model_dump()
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
                if not task["workspace"] or not task["docker_image"]:
                    raise PermissionDenied(
                        "Command requires an authorized snapshot and configured image."
                    )
                authorize(self.app, task["agent_id"], task["workspace"])
                root = self.snapshots.path(tid)
                scan(root)
                runner = DockerExecutor(
                    root, image=task["docker_image"], writable=True, limits=self.limits
                )
                # Only container argv; no host environment, host shell, pull or extra bind.
                result = asdict(
                    await asyncio.to_thread(
                        runner.execute,
                        [
                            "env",
                            "TMPDIR=/workspace/.tmp",
                            "TMP=/workspace/.tmp",
                            "TEMP=/workspace/.tmp",
                            "PYTHONPATH=/workspace/.deps/python",
                            "HOME=/workspace/.home",
                            *args["argv"],
                        ],
                    )
                )
                try:
                    scan(root)
                except (PermissionError, OSError, ValueError):
                    # The container result remains authoritative even when generated
                    # artifacts fail validation. Never lose exit/output/cleanup flags.
                    result["artifact_error"] = "unsafe_or_oversized_snapshot"
                result["verification_passed"] = bool(
                    args["purpose"] == "verify" and successful(name, result)
                )
            elif name == "save_skill":
                skills = getattr(self.app, "skills", None)
                checkpoint = self.store.load(tid)
                refs = [SourceRef(**r) for r in self.store.evidence(tid)]
                if (
                    skills is None
                    or not refs
                    or not checkpoint["verified"]
                    or checkpoint["failures"]
                ):
                    raise PermissionDenied("A skill candidate needs verified task evidence.")
                item = skills.save(
                    task["agent_id"],
                    args["name"],
                    args["description"],
                    args["instructions"],
                    refs,
                    request_id=request_id,
                )
                result = {"status": "completed", "result": item, "effect_id": step["id"]}
            elif name == "read_skill":
                skills = getattr(self.app, "skills", None)
                if skills is None:
                    raise PermissionDenied("Procedural skills are unavailable.")
                with self.app.database.transaction():
                    active = {s["id"] for s in skills.available(task["agent_id"])}
                    if args["skill_id"] not in active:
                        raise PermissionDenied("Skill is not active for this agent.")
                    item = skills.read(args["skill_id"])
                    if item["agent_id"] != task["agent_id"] or item["status"] != "active":
                        raise PermissionDenied("Skill is not active for this agent.")
                    result = {"status": "completed", "result": item, "effect_id": step["id"]}
            elif use_actions:
                if intent is None:
                    raise ValueError("Action intent was not prepared.")
                if name in MEMORY_TOOLS:
                    # Only database memory tools join this outer transaction. No
                    # network await or generic file operation runs under its lock.
                    # Keep Actions' normal lock order: a concurrent file Action
                    # must be able to finish its database writes before this read.
                    with self.app.actions.memory_reads():
                        try:
                            captured = None
                            if intent["status"] == "prepared":
                                # These four invocations are pure database reads.
                                # Capture original provenance before Action completion
                                # adds a tool source or erases a SourceInvalid error.
                                value = self.app.actions.preview_memory(
                                    task["agent_id"], name, args
                                )
                                captured = {
                                    "status": "completed",
                                    "result": json.loads(encode(value)),
                                }
                                capture_memory(self.app, task["agent_id"], name, captured)
                        except SourceInvalid:
                            result = {"status": "blocked", "error_code": "memory_changed"}
                        else:
                            result = self.app.actions.execute(
                                task["agent_id"], request_id, name, args
                            )
                            result = json.loads(encode(result))
                            if (
                                captured
                                and successful(name, result)
                                and (result["result"] == captured["result"])
                            ):
                                result.update(
                                    {k: captured[k] for k in ("memory_refs", "source_refs")}
                                )
                            else:
                                # Reconciliation uses the returned historical
                                # objects, never the current preflight content.
                                try:
                                    capture_memory(self.app, task["agent_id"], name, result)
                                except (DomainError, ValueError, TypeError, KeyError):
                                    result["memory_changed"] = True
                else:
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
            if not result.get("source") and name != "save_skill" and status != "waiting_owner":
                ref = self.app.history.add(
                    task["agent_id"],
                    encode(
                        {
                            "task_id": tid,
                            "step_id": step["id"],
                            "tool": name,
                            "purpose": step["purpose"],
                            "result": result,
                        }
                    ),
                    kind="tool",
                    scope="",
                    request_id="task-step:" + step["id"],
                )
                result["source"] = ref.model_dump()
            if result.get("source"):
                result.setdefault("source_refs", []).append(result["source"])
            self.store.result(step["id"], result, status)
            self.store.event(
                tid,
                "tool_result",
                {"step_id": step["id"], "call_id": call["id"], "name": name, "result": result},
            )
        return result
