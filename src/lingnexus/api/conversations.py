from fastapi import APIRouter, Depends, Request
from pydantic import Field

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
def edit(conversation_id: str, value: EditInput, request: Request):
    return request.app.state.services.conversations.edit_user(
        conversation_id, value.message_id, value.content, request_id=value.request_id
    )


@router.post("/conversations/{conversation_id}/head")
def select(conversation_id: str, value: HeadInput, request: Request):
    return request.app.state.services.conversations.select_head(conversation_id, value.message_id)


@router.post("/conversations/{conversation_id}/delete-turns")
def delete_turns(conversation_id: str, value: DeleteTurnsInput, request: Request):
    return request.app.state.services.conversations.delete_turns(
        conversation_id, value.anchors
    )
