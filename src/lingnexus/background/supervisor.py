"""A single background coordinator; work never belongs to a browser request."""

import asyncio
import json
from collections.abc import Callable

from lingnexus.activities import ActivityService
from lingnexus.maintenance.automatic import AutomaticBackups


class Supervisor:
    def __init__(
        self,
        app,
        learning,
        *,
        foreground_busy: Callable[[], bool] | None = None,
        interval_seconds: float = 20,
    ):
        self.app, self.learning = app, learning
        self.foreground_busy = foreground_busy or (lambda: False)
        self.interval = interval_seconds
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None
        self._work_task: asyncio.Task | None = None
        self._last_result: dict | None = None
        self.activities: ActivityService | None = None
        self.automatic_backups: AutomaticBackups | None = None

    def status(self) -> dict:
        with self.app.database.transaction() as db:
            row = db.execute("SELECT value FROM settings WHERE key='background_enabled'").fetchone()
            enabled = json.loads(row[0]) if row else True
            queued = db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
        return {
            "enabled": enabled,
            "working": self._lock.locked(),
            "queued": queued,
            "last_result": self._last_result,
            "interval_seconds": self.interval,
        }

    def configure(self, *, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("后台开关必须为布尔值")
        with self.app.database.transaction() as db:
            db.execute(
                "INSERT INTO settings VALUES ('background_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(enabled),),
            )
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
            if self.automatic_backups is not None:
                await asyncio.to_thread(self.automatic_backups.tick)
            if self.activities is not None:
                activity = await self.activities.tick()
                if activity is not None:
                    result = {"status": "activity", "activity": activity}
                    self._last_result = result
                    return result
            result = await self.learning.run_once()
            self._last_result = result
            return result

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
        await self.learning.aclose()
