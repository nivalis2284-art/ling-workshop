"""Trusted owner preparation/approval; a proposal alone never enables networking."""

from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from dataclasses import asdict
from threading import RLock

from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.tools import DockerExecutor, ExecutionLimits, LocalLimits, RestrictedLocalSandbox
from lingnexus.tools._paths import directory

from .driver import (
    check_npm_project,
    digest,
    image_id,
    prepare_directories,
    same_binding,
    snapshot_binding,
    verified_versions,
)
from .policy import DependencyCandidate, command, environment, verification_command
from .store import DependencyStore

# Multiple service instances in one parent share the in-flight fence. A new
# process has no active entries and recovers running effects to unknown.
_ACTIVE: set[tuple[str, str]] = set()
_LOCK = RLock()
_GUARDS: dict[str, asyncio.Lock] = {}


class DependencyService:
    """Parent owns the service. Only trusted owner API code may call approve/reject.

    get()/workspace() are the entire task-runtime dependency. The parent must
    serialize task resume/apply against ensure_task_ready() and approval; this
    module never mutates tasks or automatically resumes them.
    """

    def __init__(self, app, task_runtime, *, sandbox=None):
        self.app, self.task_runtime = app, task_runtime
        self.sandbox = sandbox or getattr(app, "sandbox", None)
        self.store = DependencyStore(app.database)
        self._database_key = str(app.database.path.absolute())
        self._jobs: set[asyncio.Task] = set()
        self._closed = False
        with _LOCK:
            self._parent_guard = _GUARDS.setdefault(self._database_key, asyncio.Lock())
            self.store.recover(_ACTIVE, self._database_key)

    def _level(self) -> str:
        return "container" if self.sandbox is None else self.sandbox.level()

    def _executor(self, level: str, root, plan: dict, *, readonly: bool = False):
        """Pick the install/verify executor for the current tier."""
        limits = ExecutionLimits(**plan["limits"])
        if level == "container":
            return DockerExecutor(
                root, image=plan["image_id"], writable=not readonly,
                network_mode="none" if readonly else "bridge", limits=limits,
            )
        return RestrictedLocalSandbox(
            root, writable=not readonly, limits=LocalLimits.from_execution_limits(limits)
        )

    @staticmethod
    def propose(*, manager: str, packages: list[str]) -> dict:
        """Model-safe candidate only; no persistence, Docker call or authority."""
        return DependencyCandidate.model_validate({"manager": manager, "packages": packages}).model_dump()

    def get(self, plan_id: str) -> dict:
        return self.store.get(plan_id)

    def list(self, task_id: str) -> list[dict]:
        self.task_runtime.get(task_id)
        return self.store.list(task_id)

    def ensure_task_ready(self, task_id: str) -> None:
        """Parent resume/apply guard. Unknown results require manual reconciliation."""
        self.store.assert_unfenced(task_id)

    @asynccontextmanager
    async def task_guard(self, task_id: str):
        """Parent wraps resume/apply/snapshot writes in this atomic launch guard."""
        async with self._parent_guard:
            self.ensure_task_ready(task_id)
            yield

    def _binding(self, task_id: str) -> dict:
        task = self.task_runtime.get(task_id)
        if task["status"] not in {"paused", "blocked"}:
            raise Conflict("Pause or block the task before installing dependencies.")
        if not task.get("workspace"):
            raise PermissionDenied("Dependencies require an authorized task snapshot.")
        source = directory(task["workspace"])
        grants = self.app.permissions.current(task["agent_id"])
        for kind in ("readable", "writable"):
            if not any(source.is_relative_to(directory(p)) for p in grants[kind]):
                raise PermissionDenied("Original project read/write permissions have been revoked.")
        root = directory(self.task_runtime.workspace(task_id))
        expected = self.app.database.path.absolute().parent / "task-data" / task_id / "workspace"
        if root != expected or root.is_relative_to(source) or source.is_relative_to(root):
            raise PermissionDenied("Only the task's own private snapshot can receive dependencies.")
        # Public get() exposes step_count/updated_at as the checkpoint generation.
        # No SQL/private task store/checkpoint access or mutation is needed.
        return {"task_checkpoint": task, "permissions": grants, "snapshot": snapshot_binding(root)}

    def prepare(self, task_id: str, *, manager: str, packages: list[str], request_id: str) -> dict:
        if self._closed:
            raise Conflict("Dependency service is closed.")
        candidate = DependencyCandidate.model_validate({"manager": manager, "packages": packages})
        if type(request_id) is not str or not request_id.strip() or len(request_id) > 200:
            raise ValueError("Expected a nonempty dependency request ID of at most 200 characters.")
        fingerprint = digest(candidate.model_dump())
        with _LOCK, self.app.database.transaction():
            old = self.store.lookup(task_id, request_id, fingerprint)
            if old:
                return old
            self.ensure_task_ready(task_id)
            level = self._level()
            binding = self._binding(task_id)
            if level == "container" and not binding["task_checkpoint"].get("docker_image"):
                raise PermissionDenied("Container installs need a trusted local image.")
            root = directory(binding["snapshot"]["path"])
            check_npm_project(root, candidate)
            pinned = image_id(binding["task_checkpoint"]["docker_image"], level=level)
            if level == "local":
                destination = str(root / (".deps/python" if manager == "pip" else "node_modules"))
            else:
                destination = "/workspace/.deps/python" if manager == "pip" else "/workspace/node_modules"
            plan = {
                **candidate.model_dump(), "binding": binding, "image_id": pinned,
                "execution_level": level, "destination": destination,
                "environment": environment(candidate.manager),
                "argv": command(candidate, level=level, root=root),
                "verification_argv": verification_command(candidate, level=level, root=root),
                "network_mode": "bridge", "writable": True, "container_user": "65534:65534",
                "limits": asdict(ExecutionLimits(timeout_seconds=300)),
            }
            return self.store.insert(task_id, request_id, fingerprint, plan)

    def _checked(self, plan_id: str, intent_hash: str) -> dict:
        value = self.get(plan_id)
        if (type(intent_hash) is not str or len(intent_hash) != 64
                or not hmac.compare_digest(value["intent_hash"].encode(), intent_hash.encode())
                or digest(value["plan"]) != value["intent_hash"]):
            raise Conflict("Approve the exact immutable dependency intent hash.")
        return value

    def reject(self, plan_id: str, *, intent_hash: str) -> dict:
        with _LOCK, self.app.database.transaction():
            value = self._checked(plan_id, intent_hash)
            if value["status"] != "pending":
                return value
            return self.store.result(plan_id, "failed", decision="reject", error="owner_rejected")

    async def approve(self, plan_id: str, *, intent_hash: str) -> dict:
        """Commit first, then run independently of the HTTP/browser awaiter. Never replay."""
        if self._closed:
            raise Conflict("Dependency service is closed.")
        key = (self._database_key, plan_id)
        async with self._parent_guard:
            with _LOCK:
                with self.app.database.transaction():
                    value = self._checked(plan_id, intent_hash)
                    if value["status"] != "pending":
                        return value
                    self.store.assert_unfenced(value["task_id"], excluding=plan_id)
                    same_binding(self._binding(value["task_id"]), value["plan"]["binding"])
                    self.store.result(plan_id, "running", decision="approve")
                _ACTIVE.add(key)
            # Keep a strong reference even if the owner HTTP request is cancelled.
            job = asyncio.create_task(asyncio.to_thread(self._install, value),
                                      name="ling-dependency-" + plan_id)
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)
        return await asyncio.shield(job)

    def _install(self, value: dict) -> dict:
        plan_id, plan = value["id"], value["plan"]
        key = (self._database_key, plan_id)
        level = plan.get("execution_level", "container")
        attempted = False
        verifying = False
        evidence = None
        try:
            # Queued work rechecks task ownership/permissions immediately before
            # granting bridge networking. No live project files are ever mounted.
            with self.app.database.transaction():
                same_binding(self._binding(value["task_id"]), plan["binding"])
                candidate = DependencyCandidate.model_validate({
                    "manager": plan["manager"], "packages": plan["packages"],
                })
                root = directory(plan["binding"]["snapshot"]["path"])
                if (command(candidate, level=level, root=root) != plan["argv"]
                        or environment(candidate.manager) != plan["environment"]
                        or verification_command(candidate, level=level, root=root)
                        != plan.get("verification_argv", [])):
                    raise Conflict("Dependency command policy changed; prepare a new plan.")
                check_npm_project(root, candidate)
                prepare_directories(root, candidate.manager, container=(level == "container"))
                executor = self._executor(level, root, plan)
            attempted = True
            result = executor.execute(plan["argv"])
            evidence = asdict(result)
            status = ("success" if result.status == "success" and result.exit_code == 0
                      and result.cleanup_confirmed else
                      "failed" if result.status == "failed" and
                      (result.cleanup_confirmed or not result.container_name) else "unknown")
            error = result.error or ("install_failed" if status == "failed" else None)
            if status == "success" and candidate.manager == "pip":
                verifying = True
                # Remain running/fenced until a separate read-only, offline
                # pass confirms every approved distribution in the target.
                verifier = self._executor(level, root, plan, readonly=True)
                verification = verifier.execute(plan["verification_argv"])
                evidence["verification"] = asdict(verification)
                matched = verified_versions(verification, candidate)
                if matched is None:
                    status, error = "unknown", "dependency_verification_unconfirmed"
                elif not matched:
                    status, error = "failed", "dependency_version_mismatch"
            return self.store.result(plan_id, status, result=evidence, error=error)
        except BaseException:  # noqa: BLE001 -- fence every uncertain external effect
            # Once the Docker boundary was entered, even an exception may follow
            # a completed/partial effect. Persist uncertainty and never retry.
            self.store.result(plan_id, "unknown" if attempted else "failed",
                              result=evidence,
                              error=("dependency_verification_unconfirmed" if verifying else
                                     "execution_unconfirmed" if attempted else
                                     "installation_preflight_failed"))
            return self.get(plan_id)
        finally:
            with _LOCK:
                _ACTIVE.discard(key)

    async def aclose(self):
        self._closed = True
        if self._jobs:
            await asyncio.gather(*(asyncio.shield(job) for job in list(self._jobs)),
                                 return_exceptions=True)
