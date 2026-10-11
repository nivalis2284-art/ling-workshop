from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.common.types import SourceRef

router = APIRouter(dependencies=[Depends(require_owner)])


class GoalInput(Input):
    agent_id: str
    title: str = Field(min_length=1)
    sources: list[SourceRef] = Field(min_length=1)
    request_id: str


class GoalDecision(Input):
    action: Literal["activate", "pause", "complete", "abandon"]
    expected_revision: int = Field(ge=1)


@router.get("/agents/{agent_id}/goals")
def goals(agent_id: str, request: Request):
    return request.app.state.services.development.list(agent_id)


@router.post("/goals", status_code=201)
def propose(value: GoalInput, request: Request):
    return request.app.state.services.development.propose_goal(
        value.agent_id, value.title, value.sources, request_id=value.request_id
    )


@router.post("/goals/{goal_id}/decision")
def decide(goal_id: str, value: GoalDecision, request: Request):
    return request.app.state.services.development.decide(
        goal_id, value.action, expected_revision=value.expected_revision
    )


@router.get("/agents/{agent_id}/knowledge")
def knowledge(agent_id: str, request: Request, query: str = "", limit: int = 20, offset: int = 0):
    return request.app.state.services.memory.knowledge.search(
        agent_id, query, limit=limit, offset=offset
    )
