"""Owner-only sandbox tier status and selection. Shape is consumed by the UI."""

from typing import Literal

from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(prefix="/sandbox", dependencies=[Depends(require_owner)])


class SandboxInput(Input):
    level: Literal["local", "container"]
    preset: Literal["conservative", "coding"] | None = None


def service_for(request: Request):
    return request.app.state.sandbox


@router.get("")
def status(request: Request):
    return service_for(request).info()


@router.put("")
def configure(value: SandboxInput, request: Request):
    service = service_for(request)
    service.set_level(value.level)
    if value.preset is not None:
        service.set_preset(value.preset)
    return service.info()
