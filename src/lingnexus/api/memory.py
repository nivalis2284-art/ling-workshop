"""Owner-only router; construct the domain service per request for easy integration."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, model_validator

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.memory.maintenance import MemoryMaintenance

router = APIRouter(prefix="/memory", dependencies=[Depends(require_owner)])
Kind = Literal["episodes", "knowledge"]


class EpisodeCorrection(Input):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    summary: str | None = Field(default=None, min_length=1, max_length=20000)

    @model_validator(mode="after")
    def content(self):
        changed = self.model_fields_set - {"expected_revision"}
        if not changed or any(
            getattr(self, field) is None or not getattr(self, field).strip() for field in changed
        ):
            raise ValueError("请填写要更正的内容")
        return self


class KnowledgeCorrection(Input):
    expected_revision: int = Field(ge=1)
    statement: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def content(self):
        if not self.statement.strip():
            raise ValueError("知识主张不能为空")
        return self


class StateChange(Input):
    expected_revision: int = Field(ge=1)
    action: Literal["archive", "restore", "delete"]


def service(request: Request):
    return MemoryMaintenance(request.app.state.services)


@router.get("/{agent_id}/items")
def items(
    agent_id: str,
    request: Request,
    kind: Kind = "episodes",
    scope: Literal["real", "external"] = "real",
    status: Literal["all", "active", "archived", "invalidated"] = "all",
    query: str = Query(default="", max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return service(request).items(
        agent_id, kind=kind, scope=scope, status=status, query=query, limit=limit, offset=offset
    )


@router.get("/{kind}/{item_id}")
def read(kind: Kind, item_id: str, request: Request):
    return service(request).read(kind, item_id)


@router.patch("/episodes/{item_id}")
def correct_episode(item_id: str, value: EpisodeCorrection, request: Request):
    return service(request).correct("episodes", item_id, **value.model_dump(exclude_unset=True))


@router.patch("/knowledge/{item_id}")
def correct_knowledge(item_id: str, value: KnowledgeCorrection, request: Request):
    return service(request).correct("knowledge", item_id, **value.model_dump())


@router.post("/{kind}/{item_id}/state")
def state(kind: Kind, item_id: str, value: StateChange, request: Request):
    return service(request).set_state(kind, item_id, **value.model_dump())
