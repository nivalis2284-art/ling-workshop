"""Independent durable model/tool workers; lifecycle is explicitly parent-owned."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from lingnexus.common.errors import Conflict, DomainError, PermissionDenied
from lingnexus.common.types import new_id
from lingnexus.context.builder import BudgetPolicy
from lingnexus.models import ModelGateway

from .execution import ToolExecutor, account, successful
from .protocol import append_result, dependency_update, initial, run_model
from .sandbox import SandboxService
from .store import TERMINAL, TaskStore, encode
from .workspace import Snapshots

_ACTIVE: dict[tuple[str, str], asyncio.Task] = {}


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=32_000)
    request_id: str = Field(min_length=1, max_length=200)
    workspace: str | None = None
    environment: Literal["python", "node"] = "python"


class TaskRuntime:
    """submit returns a durable ID immediately; browser disconnect never owns work.

    start recovers stale running rows to interrupted and starts queued work. Resume
    is owner-controlled. A supplied client remains caller-owned. Image/config and
    foreground_busy are trusted constructor inputs, never model tool arguments.
    """

    def __init__(
        self,
        app,
        client: httpx.AsyncClient | None = None,
        docker_image: str | None = None,
        *,
        node_image: str | None = "node:24-bookworm-slim",
        max_steps: int = 40,
        foreground_busy: Callable[[], bool] | None = None,
        limits=None,
    ):
        if type(max_steps) is not int or not 1 <= max_steps <= 1000:
            raise ValueError("Task step limit is invalid.")
        self.app = app
        self.store = TaskStore(app.database)
        self.snapshots = Snapshots(app, self.store)
        self.executor = ToolExecutor(app, self.store, self.snapshots, limits)
        self.client = client or httpx.AsyncClient()
        self._own_client = client is None
        self.gateway = ModelGateway(self.client)
        # The sandbox tier decides execution; the image is only its container input.
        self.sandbox = getattr(app, "sandbox", None) or SandboxService(app.database)
        app.sandbox = self.sandbox
        self.docker_image = docker_image or self.sandbox.image
        self.max_steps = max_steps
        self._images = {"python": self.docker_image, "node": node_image}
        self.foreground_busy = foreground_busy or (lambda: False)
        self._workers: dict[str, asyncio.Task] = {}
        self._submission_lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._closed = False
        self._db_key = str(app.database.path.absolute())

    async def start(self):
        if self._closed:
            raise Conflict("Task runtime is closed.")
        for row in self.store.pending():
            key = (self._db_key, row["id"])
            if key in _ACTIVE and not _ACTIVE[key].done():
                continue
            if row["status"] == "running":
                self.store.status(row["id"], "interrupted", "process_interrupted")
            else:
                self._spawn(row["id"])

    async def submit(self, value: dict) -> str:
        if self._closed:
            raise Conflict("Task runtime is closed.")
        spec = Submission.model_validate(value).model_dump()
        if not spec["objective"].strip():
            raise ValueError("Task objective is empty.")
        # Python preserves the original default request fingerprint, including
        # requests created before environment selection was supported.
        fingerprint_spec = {k: v for k, v in spec.items() if k != "environment" or v != "python"}
        fingerprint = hashlib.sha256(encode(fingerprint_spec).encode()).hexdigest()
        async with self._submission_lock:
            old = self.store.lookup(spec["agent_id"], spec["request_id"], fingerprint)
            if old:
                return old
            with self.app.database.transaction() as db:
                if db.execute(
                    "SELECT 1 FROM agents WHERE id=?", (spec["agent_id"],)
                ).fetchone() is None:
                    raise PermissionDenied("Individual not found.")
            pid = self.app.profiles.selection()["primary"]
            if not pid:
                raise PermissionDenied("Select a model profile before submitting a task.")
            from lingnexus.models import ModelProfile

            profile = ModelProfile.model_validate(self.app.profiles.get(pid)["profile"])
            if not profile.capabilities.tools:
                raise PermissionDenied("Task profile must support native tools.")
            tid = new_id()
            baseline = (
                self.snapshots.create(tid, spec["agent_id"], spec["workspace"])
                if spec["workspace"]
                else {}
            )
            context = self.app.context.build(
                spec["agent_id"],
                messages=[],
                budget=BudgetPolicy(
                    context_limit=profile.context_window_tokens or 32768,
                    output_reserve=profile.max_output_tokens or 4096,
                ),
            )
            system_rules = (
                context.instructions
                + "\nOwner-selected coding environment: "
                + spec["environment"]
            )
            state = {
                "environment": spec["environment"],
                "coding_workspace": bool(spec["workspace"]),
                "family": str(profile.api_family),
                "profile": profile.model_dump(mode="json"),
                "payload": initial(
                    profile,
                    spec["objective"],
                    system_rules,
                    sorted(baseline) if spec["workspace"] else None,
                ),
                "pending": [],
                "baseline": baseline,
                "failures": {},
                "writes": {},
                "checked": {},
                "verified": False,
                "command_used": False,
                "command_verified": False,
                "phase": "ready",
            }
            inserted = self.store.insert(
                tid,
                spec,
                fingerprint,
                pid,
                self._images[spec["environment"]],
                self.max_steps,
                state,
            )
            self._spawn(inserted)
            return inserted

    def _spawn(self, tid):
        key = (self._db_key, tid)
        if key in _ACTIVE and not _ACTIVE[key].done():
            return
        worker = asyncio.create_task(self._worker(tid), name="ling-task-" + tid)
        self._workers[tid] = worker
        _ACTIVE[key] = worker

    def get(self, tid):
        return self.store.get(tid)

    def list(self, agent_id=None, limit=100):
        with self.app.database.transaction() as db:
            rows = db.execute(
                "SELECT id FROM tasks WHERE (? IS NULL OR agent_id=?) "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, agent_id, max(1, min(limit, 1000))),
            )
            return [self.get(r[0]) for r in rows]

    def events(self, tid, after=0, limit=200):
        return self.store.events(tid, after, limit)

    async def stream(self, tid, after=0) -> AsyncIterator[dict]:
        """Replay a durable event cursor, then follow until an owner checkpoint."""
        while True:
            events = self.events(tid, after=after)
            for event in events:
                after = event["id"]
                yield event
            if len(events) == 200:
                continue
            if self.get(tid)["status"] not in {"queued", "running"}:
                return
            await asyncio.sleep(0.1)

    async def _stop(self, tid, status):
        self.store.status(tid, status, "process_interrupted" if status == "interrupted" else None)
        worker = _ACTIVE.get((self._db_key, tid))
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return self.get(tid)

    async def pause(self, tid):
        async with self._control_lock:
            return await self._stop(tid, "paused")

    async def cancel(self, tid):
        async with self._control_lock:
            return await self._stop(tid, "cancelled")

    async def resume(self, tid, *, additional_steps=0):
        async with self._control_lock:
            return await self._resume(tid, additional_steps)

    async def _resume(self, tid, additional_steps):
        if self._closed:
            raise Conflict("Task runtime is closed.")
        if type(additional_steps) is not int or not 0 <= additional_steps <= 1000:
            raise ValueError("Task limit extension is invalid.")
        async with self._dependency_guard(tid):
            if self.get(tid)["status"] in TERMINAL:
                raise Conflict("A terminal task cannot resume.")
            if self.get(tid)["status"] == "running":
                return self.get(tid)
            state = self.store.load(tid)
            if not dependency_update(self, tid, state):
                return self.get(tid)
            with self.app.database.transaction():
                self.store.resume(tid, additional_steps)
                self.store.save(tid, state)
            self._spawn(tid)
        return self.get(tid)

    async def steer(self, tid, text):
        """Drain bounded work, durably queue owner text, then resume if admissible."""
        if type(text) is not str or not text.strip() or len(text) > 32_000:
            raise ValueError("Owner instruction must be nonempty and at most 32000 characters.")
        async with self._control_lock:
            if self._closed or self.get(tid)["status"] in TERMINAL:
                raise Conflict("This task cannot accept owner instructions.")
            await self._stop(tid, "paused")
            if self.get(tid)["status"] in TERMINAL:
                raise Conflict("This task cannot accept owner instructions.")
            state = self.store.load(tid)
            previous = state.get("owner_instruction", "")
            instruction = previous + "\n" + text if previous else text
            if len(instruction) > 32_000:
                raise ValueError("Pending owner instructions exceed 32000 characters.")
            state["owner_instruction"] = instruction
            state.update(verified=False, command_verified=False)
            with self.app.database.transaction():
                self.store.save(tid, state)
                self.store.event(tid, "owner_instruction", {"text": text})
            try:
                return await self._resume(tid, 0)
            except Conflict:
                return self.get(tid)

    def workspace(self, tid) -> Path:
        return self.snapshots.path(tid)

    def changes(self, tid):
        return self.snapshots.changes(tid)

    def artifact(self, tid, path):
        return self.snapshots.artifact(tid, path)

    async def apply(self, tid, *, review_id):
        async with self._apply_lock, self._dependency_guard(tid):
            return self.snapshots.apply(tid, review_id)

    @asynccontextmanager
    async def _dependency_guard(self, tid):
        dependencies = getattr(self.app, "dependencies", None)
        if dependencies is None:
            yield
        else:
            async with dependencies.task_guard(tid):
                yield

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for tid, worker in list(self._workers.items()):
            if not worker.done():
                await self._stop(tid, "interrupted")
        if self._own_client:
            await self.client.aclose()

    aclose = close

    async def _priority(self, tid):
        while self.foreground_busy():
            if self.get(tid)["status"] != "running":
                return False
            await asyncio.sleep(0.1)
        return self.get(tid)["status"] == "running"

    async def _consume(self, tid, state):
        task = self.get(tid)
        call = state["pending"][0]
        work = asyncio.create_task(self.executor.execute(task, call))
        cancelled = False
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            # Docker/thread work is bounded and cannot be cancelled by killing only
            # its awaiter. Drain it and commit actual results before acknowledging pause.
            cancelled = True
            result = await work
        if result.get("status") == "prepared" and result.get("approval_id"):
            state["waiting_owner"] = {
                "approval_id": result["approval_id"],
                "browser_action_id": result["id"],
            }
            state.update(verified=False, browser_used=True, browser_verified=False)
            with self.app.database.transaction():
                self.store.save(tid, state)
                self.store.event(tid, "waiting_owner", state["waiting_owner"])
                if not cancelled:
                    self.store.status(tid, "waiting_owner")
            return False
        if result.get("status") == "unknown":
            with self.app.database.transaction():
                self.store.save(tid, state)
                self.store.status(tid, "blocked", "effect_needs_review")
            return False
        evidence_call = call
        if task["workspace"] and call["name"] in {"read_file", "create_file", "write_file"}:
            try:
                canonical = str(self.snapshots.file_path(tid, call["arguments"]["path"]))
                evidence_call = {**call, "arguments": {**call["arguments"], "path": canonical}}
            except (PermissionDenied, PermissionError, OSError, ValueError):
                pass  # Keep the failed request as unresolved evidence.
        state["coding_workspace"] = bool(task["workspace"])
        account(state, evidence_call, result)
        append_result(
            state["payload"],
            state["family"],
            call,
            encode(result),
            not successful(call["name"], result),
        )
        state["pending"].pop(0)
        state.pop("waiting_owner", None)
        state["phase"] = "ready"
        candidate = (
            result.get("dependency_candidate") if call["name"] == "request_dependencies" else None
        )
        with self.app.database.transaction():
            if candidate and successful(call["name"], result):
                dependencies = getattr(self.app, "dependencies", None)
                state["dependency_candidate"] = candidate
                state["dependencies_required"] = True
                state["dependency_baseline"] = (
                    [[p["id"], p["status"]] for p in dependencies.list(tid)] if dependencies else []
                )
                state.update(verified=False, command_verified=False, command_used=True)
                self.store.event(tid, "dependencies_required", {"dependency_candidate": candidate})
            self.store.save(tid, state)
            if candidate and not cancelled:
                self.store.status(tid, "blocked", "dependencies_required")
        if cancelled:
            raise asyncio.CancelledError
        return not candidate

    async def _model(self, tid, state):
        return await run_model(self, tid, state)

    async def _worker(self, tid):
        try:
            if not self.store.claim(tid):
                return
            state = self.store.load(tid)
            while self.get(tid)["status"] == "running":
                if not await self._priority(tid):
                    return
                if state["pending"]:
                    if not await self._consume(tid, state):
                        return
                    continue
                task = self.get(tid)
                if task["step_count"] >= task["step_limit"]:
                    self.store.status(tid, "paused", "step_limit")
                    return
                async with self._model_lock:
                    if not await self._priority(tid) or not await self._model(tid, state):
                        return
        except asyncio.CancelledError:
            if self.get(tid)["status"] == "running":
                self.store.status(tid, "interrupted", "process_interrupted")
        except PermissionDenied:
            self.store.status(tid, "blocked", "credentials_required")
        except DomainError:
            self.store.status(tid, "blocked", "task_state_invalid")
        except Exception:  # noqa: BLE001 -- worker errors must remain static and credential-free
            # No provider bodies, native exception text, paths or keys in errors.
            self.store.status(tid, "blocked", "task_runtime_error")
        finally:
            key = (self._db_key, tid)
            if _ACTIVE.get(key) is asyncio.current_task():
                _ACTIVE.pop(key, None)
            self._workers.pop(tid, None)
