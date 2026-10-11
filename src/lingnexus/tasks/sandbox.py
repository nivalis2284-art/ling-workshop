"""Sandbox tier selection: durable setting, startup engine probe, executor factory.

The tier is persisted in the settings table and defaults to the local tier. The
engine is probed but never switches the tier by itself; only an explicit owner
choice changes it.
"""

import json
from typing import Literal

from lingnexus.common.errors import PermissionDenied
from lingnexus.tools import (
    ExecutionLimits,
    LocalLimits,
    configured_image,
    create_executor,
    describe,
    probe_engine,
    resource_limits_available,
)

SETTING_KEY = "sandbox.level"
_LEVELS = ("local", "container")


class SandboxService:
    """Owner-facing tier state and the single factory for task executors."""

    def __init__(self, database, *, image: str | None = None, environ=None):
        self.database = database
        self.image = image or configured_image(environ)
        self._environ = environ
        self._engine: dict | None = None

    def probe(self) -> dict:
        self._engine = probe_engine(self._environ)
        return self._engine

    def engine(self) -> dict:
        if self._engine is None:
            return self.probe()
        return self._engine

    def level(self) -> str:
        with self.database.transaction() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
        if row is None:
            return "local"
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            return "local"
        return value if value in _LEVELS else "local"

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
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO settings VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SETTING_KEY, json.dumps(level)),
            )
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
        }

    def executor(self, worktree, *, image: str | None = None, writable: bool = False,
                 limits: ExecutionLimits | None = None,
                 network_mode: Literal["none", "bridge"] = "none"):
        """Build the executor for the current tier; callers never see the engine."""
        local_limits = LocalLimits.from_execution_limits(limits) if limits is not None else None
        return create_executor(
            self.level(), worktree, image=image or self.image, writable=writable,
            limits=limits, local_limits=local_limits, network_mode=network_mode,
        )
