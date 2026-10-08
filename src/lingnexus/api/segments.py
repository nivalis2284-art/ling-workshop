from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


class SegmentInput(Input):
    message_id: str
    title: str = Field(min_length=1, max_length=200)


class RenameInput(Input):
    title: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


@router.get("/conversations/{conversation_id}/segments")
def segments(conversation_id: str, request: Request):
    return request.app.state.services.segments.list(conversation_id)


@router.post("/conversations/{conversation_id}/segments")
def split(conversation_id: str, value: SegmentInput, request: Request):
    return request.app.state.services.segments.split(conversation_id, value.message_id, value.title)


@router.patch("/segments/{segment_id}")
def rename(segment_id: str, value: RenameInput, request: Request):
    return request.app.state.services.segments.rename(
        segment_id, value.title, expected_revision=value.expected_revision
    )
