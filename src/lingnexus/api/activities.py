"""Owner-only router; the parent mounts it and installs state.services.activities."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


class ActivityInput(Input):
    agent_id: str
    kind: Literal["reminder", "file_watch", "web_watch", "topic_review"]
    title: str = Field(min_length=1, max_length=200)
    target: str | None = Field(default=None, max_length=2048)
    due_at: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=315_360_000)


class ActivityUpdate(Input):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    target: str | None = Field(default=None, max_length=2048)
    due_at: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=315_360_000)
    status: Literal["active", "paused", "cancelled"] | None = None


class RevisionInput(Input):
    expected_revision: int = Field(ge=1)


class WebOriginsInput(Input):
    origins: list[str] = Field(max_length=100)
    expected_revision: int = Field(ge=0)


@router.get("/agents/{agent_id}/activities")
def activities(
    agent_id: str, request: Request, status: str | None = None, limit: int = 100, offset: int = 0
):
    return request.app.state.services.activities.list(
        agent_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.post("/activities", status_code=201)
def create(value: ActivityInput, request: Request):
    return request.app.state.services.activities.create(**value.model_dump(), origin="owner")


@router.get("/activities/{activity_id}")
def read(activity_id: str, request: Request):
    return request.app.state.services.activities.read(activity_id)


@router.patch("/activities/{activity_id}")
def update(activity_id: str, value: ActivityUpdate, request: Request):
    return request.app.state.services.activities.update(
        activity_id,
        **value.model_dump(exclude_unset=True),
        origin="owner",
    )


@router.post("/activities/{activity_id}/pause")
def pause(activity_id: str, value: RevisionInput, request: Request):
    return request.app.state.services.activities.pause(
        activity_id,
        expected_revision=value.expected_revision,
    )


@router.post("/activities/{activity_id}/cancel")
def cancel(activity_id: str, value: RevisionInput, request: Request):
    return request.app.state.services.activities.cancel(
        activity_id,
        expected_revision=value.expected_revision,
    )


@router.get("/activities/{activity_id}/evidence")
def evidence(activity_id: str, request: Request, limit: int = 100, offset: int = 0):
    return request.app.state.services.activities.evidence(activity_id, limit=limit, offset=offset)


@router.get("/agents/{agent_id}/activity-notifications")
def notifications(agent_id: str, request: Request, limit: int = 100, offset: int = 0):
    return request.app.state.services.activities.notifications(agent_id, limit=limit, offset=offset)


@router.get("/agents/{agent_id}/activity-web-origins")
def web_origins(agent_id: str, request: Request):
    return request.app.state.services.activities.web_origins(agent_id)


@router.put("/agents/{agent_id}/activity-web-origins")
def configure_web_origins(agent_id: str, value: WebOriginsInput, request: Request):
    return request.app.state.services.activities.configure_web_origins(
        agent_id,
        value.origins,
        expected_revision=value.expected_revision,
    )


@router.get("/agents/{agent_id}/activity-scopes")
def scopes(agent_id: str, request: Request):
    return request.app.state.services.activities.scopes(agent_id)


@router.post("/activity-scopes/{scope_id}/revoke")
def revoke_scope(scope_id: str, value: RevisionInput, request: Request):
    return request.app.state.services.activities.revoke_scope(
        scope_id,
        expected_revision=value.expected_revision,
    )
