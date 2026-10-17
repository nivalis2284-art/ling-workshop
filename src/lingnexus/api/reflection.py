"""Owner-only reflection configuration, local preview and candidate review."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.common.errors import Conflict

router = APIRouter(dependencies=[Depends(require_owner)])


class SettingsInput(Input):
    expected_revision: int = Field(ge=0)
    enabled: bool = False
    local_time: str = Field(default="04:00", pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(default="Asia/Tokyo", min_length=1, max_length=200)
    min_age_days: int = Field(default=3, ge=0, le=36600)
    threshold_bytes: int = Field(default=32768, ge=1, le=16777216)
    profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    max_calls: int = Field(default=8, ge=1, le=64)
    max_source_bytes: int = Field(default=131072, ge=1, le=67108864)
    max_events: int = Field(default=6, ge=1, le=64)
    daily_call_limit: int = Field(default=24, ge=1, le=512)


class RunInput(Input):
    request_id: str = Field(min_length=1, max_length=200)


class EventInput(Input):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2048)
    source_ids: list[str] = Field(min_length=1, max_length=512)


class PublishInput(Input):
    expected_revision: int = Field(ge=1)
    events: list[EventInput] | None = Field(default=None, max_length=64)


class QualificationInput(Input):
    expected_revision: int = Field(ge=0)
    profile_id: str = Field(min_length=1, max_length=200)
    qualified: bool
    sample_count: int = Field(ge=0)
    acceptable_count: int = Field(ge=0)
    critical_errors: int = Field(ge=0)
    evidence: str = Field(min_length=1, max_length=8192)


def runner(request):
    value = getattr(request.app.state.services, "reflections", None)
    if value is None:
        raise Conflict("反思服务尚未装配。")
    return value


@router.get("/agents/{agent_id}/reflection-settings")
def settings(agent_id: str, request: Request):
    return runner(request).settings(agent_id)


@router.put("/agents/{agent_id}/reflection-settings")
def configure(agent_id: str, value: SettingsInput, request: Request):
    return runner(request).configure(agent_id, **value.model_dump())


@router.post("/agents/{agent_id}/reflection-preview")
def preview(agent_id: str, request: Request):
    return runner(request).preview(agent_id)


@router.post("/agents/{agent_id}/reflection-runs", status_code=202)
def enqueue(agent_id: str, value: RunInput, request: Request):
    return runner(request).enqueue(agent_id, value.request_id)


@router.get("/agents/{agent_id}/reflection-runs")
def jobs(agent_id: str, request: Request):
    return runner(request).jobs(agent_id)


@router.post("/reflection-runs/{job_id}/cancel")
async def cancel(job_id: str, request: Request):
    return await runner(request).cancel(job_id)


@router.post("/reflection-runs/{job_id}/publish")
def publish(job_id: str, value: PublishInput, request: Request):
    return runner(request).publish_candidate(job_id, **value.model_dump())


@router.get("/agents/{agent_id}/reflection-qualification")
def qualification(
    agent_id: str, request: Request, profile_id: str = Query(min_length=1, max_length=200)
):
    service = runner(request)
    service._agent(agent_id)
    return service.qualification(agent_id, profile_id)


@router.put("/agents/{agent_id}/reflection-qualification")
def confirm_qualification(agent_id: str, value: QualificationInput, request: Request):
    return runner(request).confirm_qualification(agent_id, **value.model_dump())
