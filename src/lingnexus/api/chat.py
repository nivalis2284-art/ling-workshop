"""Owner-authenticated HTTP boundary for durable chat runs.

The route only validates the public request, delegates orchestration to the
runtime service, and frames already durable events as SSE. A disconnected POST
subscriber closes its event iterator; the runtime task is independent and keeps
running until completion or an explicit cancel request. The composition root
must assign a started ChatRuntime to app.state.chat_runtime and close it before
the application services.
"""

import json
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.runtime import ChatRequest, ChatRuntime
from lingnexus.runtime.store import MAX_EVENT_CURSOR

router = APIRouter(prefix="/chat", dependencies=[Depends(require_owner)])


class GuideInput(Input):
    """A mid-run instruction injected at the next step boundary."""

    text: str = Field(min_length=1, max_length=8000)


class ConfirmationInput(Input):
    """The owner's answer to a pending side-effect confirmation."""

    approved: bool


def _runtime(request: Request) -> ChatRuntime:
    """Return the lifespan-owned runtime; never create an untracked worker here."""
    runtime = getattr(request.app.state, "chat_runtime", None)
    if runtime is None:
        raise HTTPException(503, "Chat runtime is not ready.")
    return runtime


def _confirmations(request: Request):
    confirmations = getattr(request.app.state.services, "confirmations", None)
    if confirmations is None:
        raise HTTPException(503, "Confirmations are not ready.")
    return confirmations


def _sse(event: dict) -> bytes:
    """Frame one safe, JSON data record with its durable sequence id."""
    return (
        f"id: {event['id']}\n"
        f"event: {event['event']}\n"
        "data: " + json.dumps(event["data"], ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode("utf-8")


async def _stream(runtime: ChatRuntime, run_id: str, after: int = 0) -> AsyncIterator[bytes]:
    async with aclosing(runtime.events(run_id, after=after)) as events:
        async for event in events:
            yield _sse(event)


@router.post("/stream")
async def stream(value: ChatRequest, request: Request):
    """Submit or attach to a request and stream all durable events from sequence 1."""
    runtime = _runtime(request)
    run = runtime.submit(value)
    return StreamingResponse(
        _stream(runtime, run["id"]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str, request: Request, after: int = Query(default=0, ge=0, le=MAX_EVENT_CURSOR)
):
    """Replay and tail events after an exclusive durable sequence cursor."""
    runtime = _runtime(request)
    runtime.get_run(run_id)
    return StreamingResponse(
        _stream(runtime, run_id, after=after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}")
async def status(run_id: str, request: Request):
    """Read durable run status and source references after reload or completion."""
    return _runtime(request).get_run(run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str, request: Request):
    """Explicitly cancel a running turn; subscriber disconnects never call this."""
    return _runtime(request).cancel(run_id)


@router.post("/runs/{run_id}/pause")
async def pause(run_id: str, request: Request):
    """Pause at the next step boundary; an in-flight tool is never killed."""
    return _runtime(request).pause(run_id)


@router.post("/runs/{run_id}/resume")
async def resume(run_id: str, request: Request):
    """Resume a paused turn from the same step boundary."""
    return _runtime(request).resume(run_id)


@router.post("/runs/{run_id}/guide")
async def guide(run_id: str, value: GuideInput, request: Request):
    """Queue a mid-run instruction, injected at the next step boundary."""
    return _runtime(request).guide(run_id, value.text)


@router.get("/runs/{run_id}/confirmations")
async def confirmations(run_id: str, request: Request):
    """List the side-effecting calls still waiting for the owner's decision."""
    _runtime(request).get_run(run_id)
    return _confirmations(request).pending(run_id)


@router.post("/runs/{run_id}/confirmations/{fingerprint}")
async def decide(run_id: str, fingerprint: str, value: ConfirmationInput, request: Request):
    """Approve or reject one pending side-effecting call, unblocking the loop."""
    _runtime(request).get_run(run_id)
    return _confirmations(request).decide(run_id, fingerprint, approved=value.approved)
