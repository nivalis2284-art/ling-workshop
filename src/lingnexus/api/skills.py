from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.common.types import SourceRef

router = APIRouter(dependencies=[Depends(require_owner)])


class SkillInput(Input):
    agent_id: str
    name: str
    description: str
    instructions: str
    sources: list[SourceRef] = Field(min_length=1)
    request_id: str


class SkillDecision(Input):
    action: str
    expected_revision: int


class SkillRestore(Input):
    target_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


@router.get("/skills/{skill_id}/versions")
def versions(skill_id: str,request:Request):
    return request.app.state.services.skills.versions(skill_id)


@router.post("/skills/{skill_id}/restore")
def restore(skill_id:str,value:SkillRestore,request:Request):
    return request.app.state.services.skills.restore(skill_id,target_revision=value.target_revision,expected_revision=value.expected_revision)


@router.get("/agents/{agent_id}/skills")
def list_skills(agent_id: str, request: Request):
    return request.app.state.services.skills.list(agent_id)


@router.post("/skills", status_code=201)
def save(value: SkillInput, request: Request):
    return request.app.state.services.skills.save(
        value.agent_id,
        value.name,
        value.description,
        value.instructions,
        value.sources,
        request_id=value.request_id,
    )


@router.get("/skills/{skill_id}")
def read(skill_id: str, request: Request):
    return request.app.state.services.skills.read(skill_id)


@router.post("/skills/{skill_id}/decision")
def decide(skill_id: str, value: SkillDecision, request: Request):
    return request.app.state.services.skills.decide(
        skill_id, value.action, expected_revision=value.expected_revision
    )
