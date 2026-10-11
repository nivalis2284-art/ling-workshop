from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import (
    AgentInput,
    DecisionInput,
    EntryCreate,
    EntryRemove,
    InstructionsUpdate,
    ProposalCreate,
    RollbackInput,
)
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


def _services(request: Request):
    return request.app.state.services


@router.post("/agents", status_code=201)
def create_agent(value: AgentInput, request: Request):
    return {"id": request.app.state.services.create_agent(value.name)}


@router.get("/agents")
def list_agents(request: Request):
    return request.app.state.services.identity.list_agents()


@router.get("/agents/{agent_id}/state")
def get_state(agent_id: str, request: Request):
    return request.app.state.services.identity.current(agent_id)


@router.get("/agents/{agent_id}/proposals")
def list_proposals(agent_id: str, request: Request):
    return request.app.state.services.identity.list_proposals(agent_id)


@router.get("/agents/{agent_id}/revisions")
def revisions(agent_id: str, request: Request):
    return request.app.state.services.identity.revisions.list(agent_id)


@router.post("/agents/{agent_id}/rollback")
def rollback(agent_id: str, value: RollbackInput, request: Request):
    return request.app.state.services.identity.rollback(
        agent_id, target_revision=value.target_revision, expected_revision=value.expected_revision
    )


@router.post("/proposals", status_code=201)
def propose(value: ProposalCreate, request: Request):
    return request.app.state.services.identity.propose(
        value.agent_id,
        kind=value.kind,
        key=value.key,
        text=value.text,
        sources=value.sources,
        base_revision=value.base_revision,
        request_id=value.request_id,
    )


@router.post("/proposals/{proposal_id}/decision")
def decide(proposal_id: str, value: DecisionInput, request: Request):
    return request.app.state.services.identity.decide(
        proposal_id, value.decision, expected_revision=value.expected_revision
    )


@router.get("/agents/{agent_id}/instructions")
def read_instructions(agent_id: str, request: Request):
    return _services(request).instructions.view(agent_id)


@router.put("/agents/{agent_id}/instructions")
def write_instructions(agent_id: str, value: InstructionsUpdate, request: Request):
    try:
        return _services(request).instructions.replace(agent_id, value.rules)
    except TypeError as error:
        raise ValueError(str(error)) from error


@router.post("/agents/{agent_id}/instructions/reset")
def reset_instructions(agent_id: str, request: Request):
    return _services(request).instructions.reset(agent_id)


@router.get("/agents/{agent_id}/entries")
def list_entries(agent_id: str, request: Request):
    return {"agent_id": agent_id, "entries": _services(request).identity.entries(agent_id)}


@router.post("/agents/{agent_id}/entries", status_code=201)
def add_entry(agent_id: str, value: EntryCreate, request: Request):
    """Owner-written entries bypass nothing: they still carry a source and a revision."""
    services = _services(request)
    source = services.history.add(
        agent_id,
        "所有者设定或纠正这项认识：" + value.text,
        kind="user",
        request_id=value.request_id + ":source",
    )
    proposal = services.identity.propose(
        agent_id,
        kind=value.kind,
        key=value.key,
        text=value.text,
        sources=[source],
        base_revision=value.expected_revision,
        request_id=value.request_id,
    )
    if proposal["status"] == "awaiting_owner":
        services.identity.decide(
            proposal["id"], "approve", expected_revision=value.expected_revision
        )
    return {
        "state": services.identity.current(agent_id),
        "entries": services.identity.entries(agent_id),
    }


@router.post("/agents/{agent_id}/entries/remove")
def remove_entry(agent_id: str, value: EntryRemove, request: Request):
    services = _services(request)
    state = services.identity.remove(
        agent_id, key=value.key, expected_revision=value.expected_revision
    )
    return {"state": state, "entries": services.identity.entries(agent_id)}
