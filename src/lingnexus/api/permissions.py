from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.input_invalidation import cancel_inputs
from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


class PermissionInput(Input):
    readable: list[str] = Field(default_factory=list)
    writable: list[str] = Field(default_factory=list)
    expected_revision: int = Field(ge=0)


@router.get("/agents/{agent_id}/permissions")
def current(agent_id: str, request: Request):
    return request.app.state.services.permissions.current(agent_id)


@router.put("/agents/{agent_id}/permissions")
async def configure(agent_id: str, value: PermissionInput, request: Request):
    result = request.app.state.services.permissions.configure(
        agent_id,
        readable=[Path(p) for p in value.readable],
        writable=[Path(p) for p in value.writable],
        expected_revision=value.expected_revision,
    )
    await cancel_inputs(request, agent_id=agent_id)
    return result
