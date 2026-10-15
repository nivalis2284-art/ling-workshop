"""Sandbox tier selection: durable setting, startup engine probe, executor factory.

The tier is persisted in the settings table and defaults to the local tier. The
engine is probed but never switches the tier by itself; only an explicit owner
choice changes it.
"""

import asyncio
import json
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from lingnexus.common.errors import PermissionDenied
from lingnexus.common.types import utc_now
from lingnexus.common.verification import successful, workspace_version
from lingnexus.configuration.settings import read_choice, write_json
from lingnexus.tools import (
    ExecutionLimits,
    LocalLimits,
    configured_image,
    create_executor,
    describe,
    probe_engine,
    resource_limits_available,
)
from lingnexus.tools.sessions import CommandSessions

SETTING_KEY = "sandbox.level"
_LEVELS = ("local", "container")


class SandboxService:
    """Owner-facing tier state and the single factory for task executors."""

    def __init__(self, database, *, image: str | None = None, environ=None, permissions=None):
        self.database = database
        self.image = image or configured_image(environ)
        self._environ = environ
        self._engine: dict | None = None
        self.permissions = permissions
        self.sessions = CommandSessions(load=self._load_sessions, save=self._save_session)

    def _load_sessions(self):
        with self.database.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM execution_sessions")]

    def _save_session(self, record):
        with self.database.transaction() as db:
            db.execute("INSERT INTO execution_sessions VALUES(?,?,?) ON CONFLICT(id) "
                       "DO UPDATE SET record=excluded.record,updated_at=excluded.updated_at",
                       (record["session_id"], json.dumps(record, ensure_ascii=False), utc_now()))

    def preset(self):
        return read_choice(self.database, "sandbox.preset", "conservative", ("conservative", "coding"))

    def limits(self):
        return ExecutionLimits(timeout_seconds=120, memory_bytes=1_073_741_824,
                               max_output_bytes=262_144) if self.preset() == "coding" else ExecutionLimits()

    def set_preset(self, value):
        if value not in {"conservative", "coding"}:
            raise ValueError("未知的资源档位")
        write_json(self.database, "sandbox.preset", value)

    @staticmethod
    def _owner(agent_id, run_id, kind):
        return f"{kind}:{agent_id}:{run_id}"

    def task_sessions(self, agent_id, run_id):
        return self.sessions.for_run(self._owner(agent_id, run_id, "task"), run_id)

    async def stop_task_sessions(self, agent_id, run_id, *, timeout=5.0):
        owner = self._owner(agent_id, run_id, "task")
        self.sessions.cancel_run(owner, run_id)
        return await self.sessions.wait_run(owner, run_id, timeout)

    async def command(self, agent_id, run_id, kind, argv, purpose, root, *, authorization_root=None,
                      image=None, long_running=False):
        owner = self._owner(agent_id, run_id, kind)
        auth_root = Path(authorization_root or root)
        self._authorize(agent_id, auth_root)
        limits = replace(self.limits(), timeout_seconds=900) if long_running else self.limits()
        runner = self.executor(root, image=image, writable=True, limits=limits)

        def run(cancel_event, emit_output):
            # Resolve revocation immediately before entering the executor too.
            self._authorize(agent_id, auth_root)
            finished = threading.Event()
            revoked = threading.Event()

            def watch_authorization():
                while not finished.wait(0.2):
                    try:
                        self._authorize(agent_id, auth_root)
                    except PermissionDenied:
                        revoked.set()
                        cancel_event.set()
                        return

            watcher = threading.Thread(target=watch_authorization, daemon=True)
            watcher.start()
            try:
                result = asdict(runner.execute(list(argv), cancel_event=cancel_event, on_output=emit_output))
            finally:
                finished.set()
                watcher.join(timeout=1)
            if revoked.is_set():
                result["error"] = "permissions_revoked"
            result["workspace_version"] = workspace_version(Path(root))
            result["version_unavailable"] = result["workspace_version"] is None
            return result

        created = self.sessions.start(owner, run_id, list(argv), run, context={
            "root": str(root), "authorization_root": str(auth_root), "purpose": purpose,
            "agent_id": agent_id, "run_id": run_id, "kind": kind,
        })
        session_id = created["session_id"]
        # Short tools retain their familiar immediate result, slow work is observable.
        for _ in range(20):
            result = self.observe(session_id, agent_id, run_id, kind)
            if result["status"] not in {"running", "cancelling"}:
                return result
            await asyncio.sleep(0.05)
        return result

    def _authorize(self, agent_id, root):
        if self.permissions is None:
            raise PermissionDenied("执行服务缺少授权上下文。")
        grants = self.permissions.current(agent_id)
        if not any(root.is_relative_to(Path(p)) for p in grants["writable"]):
            raise PermissionDenied("命令工作目录授权已撤回。")

    def observe(self, session_id, agent_id, run_id, kind, after=0, *, cancel=False):
        owner = self._owner(agent_id, run_id, kind)
        record = self.sessions.details(session_id, owner)
        context = record["context"]
        try:
            self._authorize(agent_id, Path(context["authorization_root"]))
        except PermissionDenied:
            self.sessions.cancel(session_id, owner)
            raise
        if cancel:
            self.sessions.cancel(session_id, owner)
        view = self.sessions.poll(session_id, owner, after)
        if view["status"] not in {"running", "cancelling"}:
            record = self.sessions.details(session_id, owner)
        result = dict(view.get("result") or {"status": view["status"]})
        result["status"] = view["status"]
        private_result = record.get("result") or {}
        result.update({key: private_result[key] for key in ("workspace_version", "version_unavailable")
                       if key in private_result})
        result.update(stdout=view["output"], stderr="", execution_purpose=context["purpose"],
                      checked_argv=record["argv"], execution={**view, "truncated": view["output_truncated"]})
        result["verification_passed"] = context["purpose"] == "verify" and successful("run_command", result)
        return result

    def probe(self) -> dict:
        self._engine = probe_engine(self._environ)
        return self._engine

    def engine(self) -> dict:
        if self._engine is None:
            return self.probe()
        return self._engine

    def level(self) -> str:
        return read_choice(self.database, SETTING_KEY, "local", _LEVELS)

    def available_levels(self) -> list[str]:
        levels = ["local"]
        if self.engine()["available"]:
            levels.append("container")
        return levels

    def set_level(self, level: str) -> dict:
        if level not in _LEVELS:
            raise ValueError("未知的执行档位")
        # Re-probe so an engine started after launch can be selected.
        engine = self.probe()
        if level == "container" and not engine["available"]:
            raise PermissionDenied("本机没有可用的容器引擎，无法切换到容器隔离。")
        write_json(self.database, SETTING_KEY, level)
        return self.info()

    def info(self) -> dict:
        level = self.level()
        engine = self.engine()
        statement = describe(level, job_limits=resource_limits_available())
        limitations = list(statement["limitations"])
        if level == "container" and not engine["available"]:
            limitations.append("当前未探测到可用的容器引擎，容器档命令无法执行，请启动引擎或切回本地档")
        return {
            "level": level,
            "available_levels": self.available_levels(),
            "engine": {"available": engine["available"], "name": engine["name"]},
            "protections": statement["protections"],
            "limitations": limitations,
            "image": self.image,
            "preset": self.preset(),
            "limits": {**asdict(self.limits()), "session_timeout_seconds": 900, "max_sessions": 2},
        }

    def executor(self, worktree, *, image: str | None = None, writable: bool = False,
                 limits: ExecutionLimits | None = None,
                 network_mode: Literal["none", "bridge"] = "none"):
        """Build the executor for the current tier; callers never see the engine."""
        limits = limits or self.limits()
        local_limits = LocalLimits.from_execution_limits(limits)
        return create_executor(
            self.level(), worktree, image=image or self.image, writable=writable,
            limits=limits, local_limits=local_limits, network_mode=network_mode,
        )
