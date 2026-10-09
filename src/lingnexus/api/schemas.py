from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lingnexus.common.types import SourceRef


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentInput(Input):
    name: str = Field(min_length=1, max_length=100)


class SourceCreate(Input):
    agent_id: str
    content: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class SourceCorrection(Input):
    revision: int = Field(ge=1)
    content: str = Field(min_length=1)


class ProposalCreate(Input):
    agent_id: str
    kind: Literal[
        "habit",
        "identity",
        "relationship",
        "value",
        "major_personality",
        "major_capability",
        "custom",
    ]
    key: str
    text: str
    sources: list[SourceRef] = Field(min_length=1)
    base_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1)


class InstructionsUpdate(Input):
    rules: list[str] = Field(max_length=50)


class EntryCreate(Input):
    """Owner-written entry: goes straight through propose + approve in one call."""

    kind: Literal[
        "habit",
        "identity",
        "relationship",
        "value",
        "major_personality",
        "major_capability",
        "custom",
    ]
    key: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=6000)
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1)


class EntryRemove(Input):
    key: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)


class DecisionInput(Input):
    decision: Literal["approve", "reject"]
    expected_revision: int = Field(ge=0)


class RollbackInput(Input):
    target_revision: int = Field(ge=0)
    expected_revision: int = Field(ge=0)


class EpisodeCreate(Input):
    agent_id: str
    title: str
    summary: str
    sources: list[SourceRef] = Field(min_length=1)
    request_id: str
