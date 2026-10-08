"""Public input for a tool-free turn on a selected conversation branch."""

from typing import Annotated, Any

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from lingnexus.common.errors import DomainError
from lingnexus.common.types import Value


class ChatRequest(Value):
    """A request_id is unique per agent; exact replays attach to the original run.

    Omitted profile_id prefers the main profile, or this conversation's bound backup.
    An explicit profile other than the configured main overrides that binding.
    Replays preserve the original selection even after settings or keys change.
    Omitted conversation_id uses the agent's persistent default real conversation.
    Replay fingerprints preserve the optional input, never the resolved selection.
    Attachment IDs are ordered user-file selections. Omitted/empty lists keep
    legacy fingerprints; regeneration with an empty list reuses the original files.
    Tools and native request bodies are unsupported.
    """

    agent_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, repr=False)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    regenerate_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    attachment_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=16
    )

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict:
        result = handler(self)
        # This field did not exist on legacy requests. Keep their wire/hash form.
        if self.regenerate_message_id is None:
            result.pop("regenerate_message_id", None)
        if not self.attachment_ids:
            result.pop("attachment_ids", None)
        return result

    @field_validator("attachment_ids")
    @classmethod
    def valid_attachments(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("Attachment IDs must be unique and nonblank")
        return value

    @model_validator(mode="after")
    def regeneration_requires_conversation(self):
        if self.regenerate_message_id is not None and self.conversation_id is None:
            raise DomainError("Regeneration requires an explicit conversation_id.")
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_tools(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(
            key in value for key in ("tools", "tool_choice", "functions", "function_call")
        ):
            raise DomainError("Tools are not supported by chat yet.")
        return value

    @field_validator(
        "agent_id",
        "message",
        "request_id",
        "profile_id",
        "effort",
        "conversation_id",
        "regenerate_message_id",
    )
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Chat fields cannot be blank")
        return value
