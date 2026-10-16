from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.input_invalidation import cancel_inputs
from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


class ConversationInput(Input):
    agent_id: str
    title: str = Field(default="新的交流", min_length=1)


class EditInput(Input):
    message_id: str
    content: str = Field(min_length=1)
    request_id: str


class HeadInput(Input):
    message_id: str


class DeleteTurnsInput(Input):
    anchors: list[str] = Field(min_length=1)


class AccessInput(Input):
    mode: str


@router.get("/agents/{agent_id}/conversations")
def list_conversations(agent_id: str, request: Request):
    return request.app.state.services.conversations.list(agent_id)


@router.post("/conversations", status_code=201)
def create(value: ConversationInput, request: Request):
    return request.app.state.services.conversations.create(value.agent_id, value.title)


@router.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: str, request: Request, head_id: str | None = None):
    return request.app.state.services.conversations.messages(conversation_id, head_id=head_id)


@router.get("/conversations/{conversation_id}")
def read(conversation_id: str, request: Request):
    return request.app.state.services.conversations.read(conversation_id)


@router.get("/conversations/{conversation_id}/branches")
def branches(conversation_id: str, request: Request):
    return request.app.state.services.conversations.branches(conversation_id)


@router.post("/conversations/{conversation_id}/edit")
async def edit(conversation_id: str, value: EditInput, request: Request):
    result = request.app.state.services.conversations.edit_user(
        conversation_id, value.message_id, value.content, request_id=value.request_id
    )
    await cancel_inputs(request, conversation_id=conversation_id)
    return result


@router.post("/conversations/{conversation_id}/head")
async def select(conversation_id: str, value: HeadInput, request: Request):
    result = request.app.state.services.conversations.select_head(conversation_id, value.message_id)
    await cancel_inputs(request, conversation_id=conversation_id)
    return result


@router.post("/conversations/{conversation_id}/delete-turns")
async def delete_turns(conversation_id: str, value: DeleteTurnsInput, request: Request):
    result = request.app.state.services.conversations.delete_turns(
        conversation_id, value.anchors
    )
    await cancel_inputs(request, conversation_id=conversation_id)
    return result


@router.get("/conversations/{conversation_id}/access")
def read_access(conversation_id: str, request: Request):
    return request.app.state.services.access.get(conversation_id)


@router.post("/conversations/{conversation_id}/access")
def set_access(conversation_id: str, value: AccessInput, request: Request):
    return request.app.state.services.access.set(conversation_id, value.mode)
