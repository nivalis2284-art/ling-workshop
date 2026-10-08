"""Explicit OWNER-only plan/status/decision API; never a model tool."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.common.errors import NotFound
from lingnexus.dependencies import DependencyCandidate

router = APIRouter(prefix="/tasks/{task_id}/dependencies", dependencies=[Depends(require_owner)])


class PlanInput(DependencyCandidate):
    request_id: str = Field(min_length=1, max_length=200)


class DecisionInput(Input):
    decision: Literal["approve", "reject"]
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def service_for(request: Request):
    return request.app.state.dependencies


def scoped(service, task_id: str, plan_id: str):
    value = service.get(plan_id)
    if value["task_id"] != task_id:
        raise NotFound("Dependency plan not found for this task.")
    return value


@router.post("/plan", status_code=201)
def prepare(task_id: str, value: PlanInput, request: Request):
    return service_for(request).prepare(task_id, **value.model_dump())


@router.get("")
def plans(task_id: str, request: Request):
    return service_for(request).list(task_id)


@router.get("/{plan_id}")
def status(task_id: str, plan_id: str, request: Request):
    return scoped(service_for(request), task_id, plan_id)


@router.post("/{plan_id}/decision")
async def decision(task_id: str, plan_id: str, value: DecisionInput, request: Request):
    service = service_for(request)
    scoped(service, task_id, plan_id)
    if value.decision == "reject":
        return service.reject(plan_id, intent_hash=value.intent_hash)
    return await service.approve(plan_id, intent_hash=value.intent_hash)
