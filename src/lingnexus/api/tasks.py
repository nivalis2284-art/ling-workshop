"""Owner-only task endpoints. Parent installs router and app.state.tasks."""

import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(prefix="/tasks", dependencies=[Depends(require_owner)])


class TaskInput(Input):
    agent_id: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=32_000)
    request_id: str = Field(min_length=1, max_length=200)
    workspace: str | None = None
    environment: Literal["python", "node"] = "python"


class ResumeInput(Input):
    additional_steps: int = Field(default=0, ge=0, le=1000)


class ApplyInput(Input):
    review_id: str = Field(min_length=1)


class SteeringInput(Input):
    text: str = Field(min_length=1, max_length=16000)


@router.post("/{task_id}/steer")
async def steer(task_id: str, value: SteeringInput, request: Request):
    return await request.app.state.tasks.steer(task_id, value.text)


@router.post("", status_code=202)
async def submit(value: TaskInput, request: Request):
    tid = await request.app.state.tasks.submit(value.model_dump())
    return request.app.state.tasks.get(tid)


@router.get("")
def tasks(request: Request, agent_id: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    return request.app.state.tasks.list(agent_id, limit)


@router.get("/{task_id}")
def status(task_id: str, request: Request):
    return request.app.state.tasks.get(task_id)


@router.post("/{task_id}/pause")
async def pause(task_id: str, request: Request):
    return await request.app.state.tasks.pause(task_id)


@router.post("/{task_id}/resume")
async def resume(task_id: str, request: Request, value: ResumeInput | None = None):
    return await request.app.state.tasks.resume(
        task_id, additional_steps=value.additional_steps if value else 0
    )


@router.post("/{task_id}/cancel")
async def cancel(task_id: str, request: Request):
    return await request.app.state.tasks.cancel(task_id)


@router.get("/{task_id}/events")
def events(
    task_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
):
    return request.app.state.tasks.events(task_id, after, limit)


@router.get("/{task_id}/stream")
def stream(task_id: str, request: Request, after: int = Query(0, ge=0)):
    runtime = request.app.state.tasks
    runtime.get(task_id)

    async def generate():
        async for event in runtime.stream(task_id, after):
            yield f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.get("/{task_id}/changes")
def changes(task_id: str, request: Request):
    return request.app.state.tasks.changes(task_id)


@router.get("/{task_id}/artifacts/{path:path}")
def artifact(task_id: str, path: str, request: Request):
    return Response(
        request.app.state.tasks.artifact(task_id, path),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment"},
    )


@router.post("/{task_id}/apply")
async def apply(task_id: str, value: ApplyInput, request: Request):
    return await request.app.state.tasks.apply(task_id, review_id=value.review_id)
