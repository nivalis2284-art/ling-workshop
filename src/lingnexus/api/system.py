"""Owner-only system controls. Parent mounts router at both /system and /api/system."""

import sqlite3
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.common.errors import Conflict
from lingnexus.desktop.startup import LoginStartup
from lingnexus.maintenance.automatic import AutomaticBackups
from lingnexus.maintenance.restore import RestoreQueue, maintenance_lock, plain_path

router = APIRouter(prefix="/system", dependencies=[Depends(require_owner)])


class BackupSettings(Input):
    enabled: bool | None = None
    path: str | None = Field(default=None, min_length=1)
    retention: int | None = Field(default=None, ge=1, le=365)


class StartupSettings(Input):
    enabled: bool


class RestoreSource(Input):
    source_format: Literal["backup", "export"]
    source_path: str = Field(min_length=1)


class RestoreConfirmation(Input):
    plan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmed: Literal[True]


class SystemAPI:
    """Optional explicit composition hook; lazy routing also works with state.services."""

    def __init__(self, app, *, startup: LoginStartup | None = None):
        self.automatic = getattr(app, "automatic_backups", None) or AutomaticBackups(app)
        app.automatic_backups = self.automatic
        self.restore = RestoreQueue(app)
        self.startup = startup or LoginStartup(app.database.path.parent)


def _system(request: Request) -> SystemAPI:
    services = request.app.state.services
    with maintenance_lock(plain_path(services.database.path.parent)):
        if not hasattr(request.app.state, "system_api"):
            request.app.state.system_api = SystemAPI(
                services, startup=getattr(request.app.state, "login_startup", None)
            )
        return request.app.state.system_api


def _perform(operation):
    try:
        return operation()
    except Conflict as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, KeyError, TypeError, sqlite3.DatabaseError) as error:
        raise HTTPException(422, "设置或恢复资料无效，请检查后重新预览。") from error
    except OSError as error:
        raise HTTPException(409, "无法访问维护文件，或目标已存在；原资料已保留。") from error


def _quit_available(request: Request) -> bool:
    return callable(getattr(request.app.state, "request_quit", None))


@router.get("/backups")
def backup_status(request: Request):
    return _perform(lambda: _system(request).automatic.status())


@router.put("/backups")
def configure_backups(value: BackupSettings, request: Request):
    return _perform(
        lambda: _system(request).automatic.configure(**value.model_dump(exclude_none=True))
    )


@router.get("/startup")
def startup_status(request: Request):
    def operation():
        startup = _system(request).startup
        return {"available": startup.available, "enabled": startup.enabled()}

    return _perform(operation)


@router.put("/startup")
def configure_startup(value: StartupSettings, request: Request):
    def operation():
        startup = _system(request).startup
        startup.set_enabled(value.enabled)
        return {"available": startup.available, "enabled": startup.enabled()}

    return _perform(operation)


@router.get("/restore")
def restore_status(request: Request):
    return _perform(lambda: _system(request).restore.status()) | {
        "quit_available": _quit_available(request)
    }


@router.post("/restore/prepare")
def prepare_restore(value: RestoreSource, request: Request):
    return _perform(
        lambda: _system(request).restore.prepare(value.source_format, value.source_path)
    )


@router.post("/restore/commit")
def commit_restore(value: RestoreConfirmation, request: Request):
    return _perform(
        lambda: _system(request).restore.commit(value.plan_id, confirmed=value.confirmed)
    ) | {
        "quit_available": _quit_available(request),
    }


@router.post("/restore/quit")
def quit_after_restore(value: RestoreConfirmation, request: Request, tasks: BackgroundTasks):
    status = _perform(lambda: _system(request).restore.status())
    if status["status"] != "queued" or status["plan_id"] != value.plan_id:
        raise HTTPException(409, "请先确认并排队恢复计划。")
    available = _quit_available(request)
    if available:
        tasks.add_task(request.app.state.request_quit)
    return {"quit_requested": available, "restart_required": True}
