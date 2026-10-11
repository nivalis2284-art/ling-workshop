from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(prefix="/background", dependencies=[Depends(require_owner)])


class BackgroundInput(Input):
    enabled: bool


@router.get("")
def status(request: Request):
    return request.app.state.background.status()


@router.put("")
async def configure(value: BackgroundInput, request: Request):
    return request.app.state.background.configure(enabled=value.enabled)


@router.post("/run-once")
async def run_once(request: Request):
    return await request.app.state.background.run_now()
