"""Minimal owner-only individual registry and system-rule editing.

The first part needs only an id and a name: this router writes the agents table
directly through the composition root and never touches the long-term part.
"""

from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import AgentInput, InstructionsUpdate
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


@router.post("/agents", status_code=201)
def create_agent(value: AgentInput, request: Request):
    return {"id": request.app.state.services.create_agent(value.name)}


@router.get("/agents")
def list_agents(request: Request):
    return request.app.state.services.list_agents()


@router.get("/agents/{agent_id}/instructions")
def read_instructions(agent_id: str, request: Request):
    return request.app.state.services.instructions.view(agent_id)


@router.put("/agents/{agent_id}/instructions")
def write_instructions(agent_id: str, value: InstructionsUpdate, request: Request):
    try:
        return request.app.state.services.instructions.replace(agent_id, value.rules)
    except TypeError as error:
        raise ValueError(str(error)) from error


@router.post("/agents/{agent_id}/instructions/reset")
def reset_instructions(agent_id: str, request: Request):
    return request.app.state.services.instructions.reset(agent_id)
