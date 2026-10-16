"""Owner event controls; UI history reads never invoke a model."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.common.errors import Conflict

router = APIRouter(dependencies=[Depends(require_owner)])


class BeginInput(Input):
    title: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=200)
    start_message_id: str | None = None


class RevisionInput(Input):
    expected_revision: int = Field(ge=0)


class FoldInput(RevisionInput):
    folded: bool


class SummaryInput(RevisionInput):
    text: str = Field(min_length=1, max_length=2048)


class LinkInput(RevisionInput):
    task_id: str = Field(min_length=1, max_length=200)
    origin_message_id: str | None = None


class SummarySettingsInput(Input):
    enabled: bool = False
    profile_id: str | None = None
    max_calls: int = Field(default=4, ge=1, le=8)


def _events(request: Request):
    return request.app.state.services.work_events


def _scope(request: Request, event_id: str):
    value = _events(request).get(event_id)
    return {"agent_id": value["agent_id"], "conversation_id": value["conversation_id"]}


def _runner(request: Request):
    runner = getattr(request.app.state.services, "event_summaries", None)
    if runner is None:
        raise Conflict("事件整理服务尚未启动。")
    return runner


@router.get("/conversations/{conversation_id}/work-events")
def list_events(conversation_id: str, request: Request):
    return _events(request).list(conversation_id)


@router.post("/conversations/{conversation_id}/work-events", status_code=201)
def begin(conversation_id: str, value: BeginInput, request: Request):
    return _events(request).begin(
        conversation_id, value.title, value.request_id,
        start_message_id=value.start_message_id,
    )


@router.get("/work-events/{event_id}")
def get_event(event_id: str, request: Request):
    return _events(request).get(event_id)


@router.post("/work-events/{event_id}/close")
def close(event_id: str, value: RevisionInput, request: Request):
    event = _events(request).close(event_id, expected_revision=value.expected_revision)
    runner = getattr(request.app.state.services, "event_summaries", None)
    if runner is not None and runner.settings(event["agent_id"]).get("enabled"):
        runner.enqueue(event_id)
    return event


@router.post("/work-events/{event_id}/fold")
def fold(event_id: str, value: FoldInput, request: Request):
    return _events(request).set_fold(event_id, value.folded, expected_revision=value.expected_revision)


@router.post("/work-events/{event_id}/summary")
def summary(event_id: str, value: SummaryInput, request: Request):
    return _events(request).set_summary(event_id, value.text, expected_revision=value.expected_revision)


@router.post("/work-events/{event_id}/tasks")
def link_task(event_id: str, value: LinkInput, request: Request):
    return _events(request).link_task(
        event_id, value.task_id, expected_revision=value.expected_revision,
        origin_message_id=value.origin_message_id,
    )


@router.get("/work-events/{event_id}/records")
def directory(
    event_id: str, request: Request,
    cursor: str | None = Query(None, max_length=4096),
    limit: int = Query(20, ge=1, le=20),
):
    return _events(request).directory(event_id, **_scope(request, event_id), cursor=cursor, limit=limit)


@router.get("/work-events/{event_id}/records/{source_id}")
def read_record(
    event_id: str, source_id: str, request: Request,
    cursor: str | None = Query(None, max_length=4096),
    max_bytes: int = Query(8192, ge=1, le=16384),
):
    return _events(request).read_record(
        event_id, source_id, **_scope(request, event_id), cursor=cursor, max_bytes=max_bytes,
    )


@router.get("/agents/{agent_id}/work-event-settings")
def summary_settings(agent_id: str, request: Request):
    return _runner(request).settings(agent_id)


@router.put("/agents/{agent_id}/work-event-settings")
def configure_summary(agent_id: str, value: SummarySettingsInput, request: Request):
    return _runner(request).configure(agent_id, **value.model_dump())


@router.post("/work-events/{event_id}/summarize", status_code=202)
def enqueue_summary(event_id: str, value: RevisionInput, request: Request):
    event = _events(request).get(event_id)
    if event["revision"] != value.expected_revision:
        raise Conflict("工作事件版本已改变，请刷新后重试。")
    return _runner(request).enqueue(event_id)


@router.get("/work-events/{event_id}/summary-jobs")
def summary_jobs(event_id: str, request: Request):
    _events(request).get(event_id)
    return _runner(request).jobs(event_id)
