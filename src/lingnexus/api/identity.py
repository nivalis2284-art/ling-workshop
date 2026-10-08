from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import AgentInput, DecisionInput, ProposalCreate, RollbackInput
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


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
