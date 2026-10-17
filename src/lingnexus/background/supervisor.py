"""A single background coordinator; work never belongs to a browser request.

The first part drives backups and optional bounded work-event summaries. The long-term part's
consolidation worker and proactive attention are not assembled and must not be
referenced here.
"""

import asyncio
from collections.abc import Callable

from lingnexus.configuration.settings import read_json, write_json
from lingnexus.maintenance.automatic import AutomaticBackups


class Supervisor:
    def __init__(
        self,
        app,
        *,
        foreground_busy: Callable[[], bool] | None = None,
        interval_seconds: float = 20,
    ):
        self.app = app
        self.foreground_busy = foreground_busy or (lambda: False)
        self.interval = interval_seconds
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None
        self._work_task: asyncio.Task | None = None
        self._last_result: dict | None = None
        self.automatic_backups: AutomaticBackups | None = None
        self.event_summaries = None
        self.reflections = None

    def status(self) -> dict:
        enabled = read_json(self.app.database, "background_enabled", True)
        return {
            "enabled": enabled,
            "working": self._lock.locked(),
            "last_result": self._last_result,
            "interval_seconds": self.interval,
        }

    def configure(self, *, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("后台开关必须为布尔值")
        write_json(self.app.database, "background_enabled", enabled)
        if not enabled and self._work_task and not self._work_task.done():
            self._work_task.cancel()
        return self.status()

    async def run_now(self) -> dict:
        if not self.status()["enabled"]:
            return {"status": "paused"}
        if self.foreground_busy():
            return {"status": "foreground_busy"}
        if self._lock.locked():
            return {"status": "busy"}
        async with self._lock:
            if self.automatic_backups is None and self.event_summaries is None and self.reflections is None:
                return {"status": "unavailable"}
            reflection = await self.reflections.tick() if self.reflections else None
            summary = await self.event_summaries.tick() if self.event_summaries else None
            result = await asyncio.to_thread(self.automatic_backups.tick) if self.automatic_backups else {}
            self._last_result = {"status": "ok", "backup_created": bool(result.get("created"))}
            if summary is not None:
                self._last_result["event_summary"] = summary
            if reflection is not None:
                self._last_result["reflection"] = reflection
            return self._last_result

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._work_task = asyncio.create_task(self.run_now())
            try:
                await self._work_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                # An owner pause cancels just the work, not the coordinator.
            except Exception:  # noqa: BLE001 -- no secrets or raw exceptions in status
                self._last_result = {"status": "failed", "error_code": "background_error"}
            finally:
                self._work_task = None

    async def aclose(self) -> None:
        for task in (self._work_task, self._loop_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._work_task, self._loop_task) if t), return_exceptions=True
        )
