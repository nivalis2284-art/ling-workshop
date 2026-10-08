from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def new_id() -> str:
    return str(uuid4())


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceRef(Value):
    id: str = Field(min_length=1)
    revision: int = Field(ge=1)


SourceKind = Literal["user", "assistant", "observation", "tool", "system"]


class SourceInput(Value):
    content: str = Field(min_length=1)
    kind: SourceKind
    scope: str = Field(pattern=r"^(real|external|test|scene:[a-zA-Z0-9_-]+)$")
    request_id: str = Field(min_length=1)


class SelfSnapshot(Value):
    agent_id: str
    revision: int
    entries: dict[str, str]
