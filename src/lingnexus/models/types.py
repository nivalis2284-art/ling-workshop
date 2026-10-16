"""Public, storage-independent types for the bounded native model gateway."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIFamily(StrEnum):
    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"


ENDPOINTS = {
    APIFamily.ANTHROPIC_MESSAGES: "messages",
    APIFamily.OPENAI_CHAT_COMPLETIONS: "chat/completions",
    APIFamily.OPENAI_RESPONSES: "responses",
}


class ModelCapabilities(BaseModel):
    """Caller-declared capabilities, never inferred from a model's name.

    An empty effort_levels tuple means effort selection is unsupported. Thinking
    describes request support, not access to hidden reasoning. encrypted_reasoning
    gates requesting Responses' reasoning.encrypted_content include field.
    Sampling and prompt caching default to unsupported, including reasoning
    models that expose effort but reject sampling controls.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    streaming: bool = True
    tools: bool = True
    thinking: bool = False
    encrypted_reasoning: bool = False
    images: bool = Field(default=False, strict=True)
    pdf: bool = Field(default=False, strict=True)
    effort_levels: tuple[str, ...] = ()
    temperature: bool = Field(default=False, strict=True)
    top_p: bool = Field(default=False, strict=True)
    prompt_caching: bool = Field(default=False, strict=True)


class ModelProfile(BaseModel):
    """Serializable model configuration containing no credentials.

    None means unknown for limits/prices (USD per million tokens). No pricing
    catalog or model capability guesses are embedded in this transport spike.
    Root URLs receive /v1; explicit proxy/version paths are preserved. An already
    complete native endpoint is accepted without duplicating its suffix.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    api_family: APIFamily
    model: str = Field(min_length=1)
    base_url: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    context_window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)
    default_effort: str | None = Field(default=None, strict=True)
    temperature: float | None = Field(default=None, strict=True, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, strict=True, ge=0, le=1, allow_inf_nan=False)
    cache_system_prompt: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_generation_settings(self) -> "ModelProfile":
        if (
            self.default_effort is not None
            and self.default_effort not in self.capabilities.effort_levels
        ):
            raise ValueError("Default effort must be a declared effort level")
        for name in ("temperature", "top_p"):
            if getattr(self, name) is not None and not getattr(self.capabilities, name):
                raise ValueError("Sampling settings require declared support")
        if (
            self.api_family == APIFamily.ANTHROPIC_MESSAGES
            and self.temperature is not None
            and self.temperature > 1
        ):
            raise ValueError("Anthropic temperature must be between 0 and 1")
        if self.cache_system_prompt and (
            self.api_family != APIFamily.ANTHROPIC_MESSAGES or not self.capabilities.prompt_caching
        ):
            raise ValueError("System prefix caching requires declared Anthropic support")
        return self

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        try:
            parts = urlsplit(value)
            valid_port = parts.port is None or 0 < parts.port < 65536
        except ValueError:
            raise ValueError("Invalid model endpoint URL") from None
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or not valid_port
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("Use an HTTP(S) URL without credentials, query, or fragment")
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    @property
    def endpoint_url(self) -> str:
        parts = urlsplit(self.base_url)
        path = parts.path.rstrip("/") or "/v1"
        suffix = "/" + ENDPOINTS[self.api_family]
        if not path.endswith(suffix):
            path += suffix
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True, slots=True)
class RuntimeCredentials:
    """Pass per call. Never put this object in configuration or persistence."""

    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class StreamLimits:
    max_event_bytes: int = 1024 * 1024
    max_stream_bytes: int = 32 * 1024 * 1024
    max_events: int = 100_000

    def __post_init__(self) -> None:
        if min(self.max_event_bytes, self.max_stream_bytes, self.max_events) <= 0:
            raise ValueError("Stream limits must be positive")


@dataclass(frozen=True, slots=True)
class Usage:
    """Cumulative snapshots; absent counts and unknown cost stay None."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str = field(repr=False)
    arguments: dict[str, Any] = field(repr=False)
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderContinuation:
    """Native material, separate from display events; never automatically replayed.

    response is the native response (assembled for Messages/Chat); events keeps
    original JSON objects, including unknown fields and opaque deltas. Snapshots
    are detached from parser state. On errors they may be partial and are not
    guaranteed replayable. Raw provider data is intentionally excluded from repr.
    """

    api_family: APIFamily
    response_id: str | None
    response: dict[str, Any] = field(repr=False)
    events: tuple[dict[str, Any], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelError:
    code: str
    message: str
    status_code: int | None = None
    reason: str | None = None


EventType = Literal[
    "text_delta", "thinking_delta", "tool_call_delta", "tool_call", "usage", "complete", "error"
]


@dataclass(frozen=True, slots=True)
class ModelEvent:
    type: EventType
    text: str | None = field(default=None, repr=False)
    index: int | None = None
    item_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = field(default=None, repr=False)
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error: ModelError | None = None
    continuation: ProviderContinuation | None = field(default=None, repr=False)
