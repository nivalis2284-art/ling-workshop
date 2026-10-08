from pathlib import Path

from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(prefix="/maintenance", dependencies=[Depends(require_owner)])


class Destination(Input):
    path: str


@router.post("/export")
def export(value: Destination, request: Request):
    return request.app.state.services.maintenance.export_to(Path(value.path))


@router.post("/backup")
def backup(value: Destination, request: Request):
    request.app.state.services.maintenance.backup_to(Path(value.path))
    return {"saved": True}
