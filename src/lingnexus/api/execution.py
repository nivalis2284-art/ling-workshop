"""Owner-authenticated observation of run-scoped command sessions."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(prefix="/execution", dependencies=[Depends(require_owner)])


class Control(Input):
    run_id: str
    kind: Literal["chat", "task"] = "chat"


def _observe(request, session_id, run_id, kind, after=0, cancel=False):
    state = request.app.state
    run = state.chat_runtime.get_run(run_id) if kind == "chat" else state.tasks.get(run_id)
    result = state.sandbox.observe(session_id, run["agent_id"], run_id, kind, after, cancel=cancel)
    return result["execution"]


@router.get("/sessions/{session_id}")
async def poll(session_id: str, run_id: str, request: Request,
               kind: Literal["chat", "task"] = "chat", after: int = Query(default=0, ge=0)):
    return _observe(request, session_id, run_id, kind, after)


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str, value: Control, request: Request):
    return _observe(request, session_id, value.run_id, value.kind, cancel=True)
