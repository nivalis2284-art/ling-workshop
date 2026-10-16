"""Direct httpx transport; one native request, no agent loop or persistence."""

import asyncio
import math
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx

from ._parsers import AnthropicParser, ChatParser, Parser, ResponsesParser, obj
from ._sse import ProtocolError, frames, json_object
from .types import APIFamily, ModelError, ModelEvent, ModelProfile, RuntimeCredentials, StreamLimits

_INHERITED_AUTH_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "x-auth-token",
    "x-litellm-api-key",
    "x-portkey-api-key",
    "cookie",
)

ERROR_MESSAGES = {
    "invalid_request": "Request or credentials are invalid for this model profile.",
    "http_error": "The model endpoint returned an unsuccessful HTTP status.",
    "content_type": "The model endpoint did not return an SSE stream.",
    "transport_error": "The model connection failed.",
    "timeout": "The model stream exceeded its time limit.",
    "malformed_stream": "The model stream contained malformed or inconsistent data.",
    "unexpected_eof": "The model stream ended without a native terminal event.",
    "stream_limit": "The model stream exceeded its configured size or event limit.",
    "provider_error": "The provider reported a model error.",
    "incomplete_response": "The provider marked the response incomplete.",
    "invalid_tool_arguments": "A tool call did not contain a valid JSON object.",
}


def _priced_event(profile: ModelProfile, event: ModelEvent) -> ModelEvent:
    """Estimate at emission time; native usage and continuation stay untouched."""
    usage = event.usage
    if usage is None:
        return event
    cost = None
    if (
        profile.input_price_per_million is not None
        and profile.output_price_per_million is not None
        and usage.input_tokens is not None
        and usage.output_tokens is not None
        and usage.cached_input_tokens in (None, 0)
        and usage.cache_creation_input_tokens in (None, 0)
    ):
        cost = (
            Decimal(usage.input_tokens) * profile.input_price_per_million
            + Decimal(usage.output_tokens) * profile.output_price_per_million
        ) / Decimal(1_000_000)
    return replace(event, usage=replace(usage, cost_usd=cost))


def _sampling_settings(profile: ModelProfile, body: dict[str, Any]) -> None:
    for name in ("temperature", "top_p"):
        default = getattr(profile, name)
        if name not in body and default is None:
            continue
        value = body.get(name, default)
        upper = (
            2 if name == "temperature" and profile.api_family != APIFamily.ANTHROPIC_MESSAGES else 1
        )
        if (
            not getattr(profile.capabilities, name)
            or type(value) not in (int, float)
            or not 0 <= value <= upper
            or not math.isfinite(value)
            or (default is not None and name in body and value != default)
        ):
            raise ProtocolError("invalid_request")
        body[name] = value


def _cache_system_prefix(profile: ModelProfile, body: dict[str, Any]) -> None:
    # Check only native breakpoint locations, not user text or tool input
    # schemas that happen to contain a property named cache_control.
    locations = [body]
    for field in ("system", "tools"):
        if isinstance(body.get(field), list):
            locations.extend(body[field])
    for message in body.get("messages", []):
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            locations.extend(message["content"])
    if not profile.capabilities.prompt_caching and any(
        isinstance(block, dict) and block.get("cache_control") is not None for block in locations
    ):
        raise ProtocolError("invalid_request")
    if not profile.cache_system_prompt:
        return
    if (
        profile.api_family != APIFamily.ANTHROPIC_MESSAGES
        or not profile.capabilities.prompt_caching
    ):
        raise ProtocolError("invalid_request")
    # This opt-in owns only an explicit system breakpoint, never automatic
    # request-level caching or native message/thinking/tool continuation blocks.
    if body.get("cache_control") is not None:
        raise ProtocolError("invalid_request")
    system = body.get("system")
    if system is None or system == "" or system == []:
        return
    blocks: list[dict[str, Any]]
    if isinstance(system, str):
        blocks = [{"type": "text", "text": system}]
    elif isinstance(system, list):
        blocks = system
    else:
        raise ProtocolError("invalid_request")
    if any(
        not isinstance(block, dict)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
        or not block["text"]
        for block in blocks
    ):
        raise ProtocolError("invalid_request")
    marker = {"type": "ephemeral"}
    if blocks[-1].get("cache_control") not in (None, marker):
        raise ProtocolError("invalid_request")
    blocks[-1]["cache_control"] = marker
    body["system"] = blocks


def _request_body(
    profile: ModelProfile, payload: Mapping[str, Any], effort: str | None
) -> dict[str, Any]:
    body = deepcopy(dict(payload))
    caps = profile.capabilities
    if not caps.streaming or (body.get("tools") and not caps.tools):
        raise ProtocolError("invalid_request")
    if body.get("model", profile.model) != profile.model or body.get("stream", True) is not True:
        raise ProtocolError("invalid_request")
    if any(key in body for key in ("api_key", "authorization", "credentials")):
        raise ProtocolError("invalid_request")
    body.update(model=profile.model, stream=True)
    _sampling_settings(profile, body)
    # Reject misplaced effort fields instead of forwarding an unvalidated
    # setting from another API family to a compatible proxy.
    if profile.api_family != APIFamily.OPENAI_CHAT_COMPLETIONS and "reasoning_effort" in body:
        raise ProtocolError("invalid_request")
    for field, family in (
        ("reasoning", APIFamily.OPENAI_RESPONSES),
        ("output_config", APIFamily.ANTHROPIC_MESSAGES),
    ):
        if profile.api_family != family and "effort" in obj(body.get(field) or {}):
            raise ProtocolError("invalid_request")
    if profile.api_family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        if body.get("n", 1) != 1:
            raise ProtocolError("invalid_request")
        stream_options = obj(body.get("stream_options") or {})
        stream_options["include_usage"] = True
        body["stream_options"] = stream_options
        effort_container = body
        effort_key = "reasoning_effort"
    else:
        field = "reasoning" if profile.api_family == APIFamily.OPENAI_RESPONSES else "output_config"
        effort_container = obj(body.get(field) or {})
        effort_key = "effort"
    requested_effort = effort_container.get(effort_key)
    if effort_key in effort_container and (
        not isinstance(requested_effort, str) or requested_effort not in caps.effort_levels
    ):
        raise ProtocolError("invalid_request")
    if effort is not None:
        if requested_effort is not None and requested_effort != effort:
            raise ProtocolError("invalid_request")
        requested_effort = effort
    elif requested_effort is None:
        requested_effort = profile.default_effort
    if requested_effort is not None and requested_effort not in caps.effort_levels:
        raise ProtocolError("invalid_request")
    if requested_effort is not None:
        effort_container[effort_key] = requested_effort
        if profile.api_family != APIFamily.OPENAI_CHAT_COMPLETIONS:
            body[field] = effort_container
    if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
        if not isinstance(body.get("messages"), list):
            raise ProtocolError("invalid_request")
        if type(body.get("max_tokens")) is not int or body["max_tokens"] <= 0:
            raise ProtocolError("invalid_request")
        if (
            body.get("thinking")
            and obj(body["thinking"]).get("type") != "disabled"
            and not caps.thinking
        ):
            raise ProtocolError("invalid_request")
        _cache_system_prefix(profile, body)
    elif profile.api_family == APIFamily.OPENAI_CHAT_COMPLETIONS:
        if not isinstance(body.get("messages"), list):
            raise ProtocolError("invalid_request")
    else:
        if "input" not in body and "previous_response_id" not in body:
            raise ProtocolError("invalid_request")
        if obj(body.get("reasoning") or {}).get("summary") is not None and not caps.thinking:
            raise ProtocolError("invalid_request")
        if (
            "reasoning.encrypted_content" in body.get("include", [])
            and not caps.encrypted_reasoning
        ):
            raise ProtocolError("invalid_request")
    return body


class ModelGateway:
    """Async, bounded native transport with a caller-owned httpx client.

    stream(profile, payload, *, credentials, effort=None) yields ModelEvent.
    payload is a provider-native JSON mapping, copied before changes. The gateway
    sets model/stream and requests Chat usage. Effort must be explicitly allowed
    by the profile and maps to the family's native field. Explicit effort
    overrides the profile default; sampling and system caching require declared
    support. Usage cost is estimated using this call's prices only when both
    counts/prices are known and no cache tokens are reported. Tool schema validation,
    executing tools, replay policy, persistence, retries and model catalogs belong
    to callers. A finished stream has exactly one complete or error event.

    CancelledError propagates. Closing the iterator closes the HTTP response but
    never the injected client; use contextlib.aclosing when breaking iteration.
    No credentials or response bodies are logged. Error messages are static to
    avoid reflecting a key from provider errors or transport exceptions.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        limits: StreamLimits | None = None,
        timeout_seconds: float = 60,
        total_timeout_seconds: float = 300,
    ) -> None:
        if any(not math.isfinite(t) or t <= 0 for t in (timeout_seconds, total_timeout_seconds)):
            raise ValueError("Timeouts must be finite and positive")
        self._client = client
        self._limits = limits or StreamLimits()
        self._timeout = timeout_seconds
        self._total_timeout = total_timeout_seconds

    async def stream(
        self,
        profile: ModelProfile,
        payload: Mapping[str, Any],
        *,
        credentials: RuntimeCredentials,
        effort: str | None = None,
    ) -> AsyncGenerator[ModelEvent, None]:
        parser: AnthropicParser | ChatParser | ResponsesParser
        parser = {
            APIFamily.ANTHROPIC_MESSAGES: AnthropicParser,
            APIFamily.OPENAI_CHAT_COMPLETIONS: ChatParser,
            APIFamily.OPENAI_RESPONSES: ResponsesParser,
        }[profile.api_family]()
        try:
            try:
                body = _request_body(profile, payload, effort)
                key = credentials.api_key
                if not key or not key.isascii() or any(c.isspace() or ord(c) < 33 for c in key):
                    raise ProtocolError("invalid_request")
            except (TypeError, ValueError, ProtocolError):
                raise ProtocolError("invalid_request") from None
            headers = {"accept": "text/event-stream"}
            if profile.api_family == APIFamily.ANTHROPIC_MESSAGES:
                headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            else:
                headers["authorization"] = f"Bearer {key}"
            deadline = asyncio.get_running_loop().time() + self._total_timeout
            async with aclosing(self._exchange(profile, body, headers, parser)) as exchange:
                while True:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
                    try:
                        # A timeout context must not span a yield to application
                        # code: it would cancel the consuming task while paused.
                        async with asyncio.timeout_at(deadline):
                            event = await anext(exchange)
                    except StopAsyncIteration:
                        return
                    if event.type in ("complete", "error"):
                        await exchange.aclose()
                    yield _priced_event(profile, event)
                    if event.type in ("complete", "error"):
                        return
        except ProtocolError as exc:
            yield _priced_event(profile, self._error(parser, exc.code, reason=exc.reason))
        except (TimeoutError, httpx.TimeoutException):
            yield _priced_event(profile, self._error(parser, "timeout"))
        except httpx.HTTPError:
            yield _priced_event(profile, self._error(parser, "transport_error"))
        except (ValueError, TypeError, RecursionError):
            # JSON serialization rejects invalid native request values. Never
            # include exception text: it can contain caller-supplied credentials.
            yield _priced_event(profile, self._error(parser, "invalid_request"))

    async def _exchange(
        self,
        profile: ModelProfile,
        body: dict[str, Any],
        headers: dict[str, str],
        parser: AnthropicParser | ChatParser | ResponsesParser,
    ) -> AsyncGenerator[ModelEvent, None]:
        request = self._client.build_request(
            "POST",
            profile.endpoint_url,
            json=body,
            timeout=self._timeout,
        )
        # Even params={} would merge client query defaults. Use only the
        # validated profile URL so credentials cannot cross provider boundaries.
        request.url = httpx.URL(profile.endpoint_url)
        # build_request merges client headers and cookies. Sanitize only this
        # request before applying runtime auth; shared defaults stay untouched.
        for name in _INHERITED_AUTH_HEADERS:
            request.headers.pop(name, None)
        request.headers.update(headers)
        response = await self._client.send(request, stream=True, follow_redirects=False, auth=None)
        async with aclosing(response):
            if not 200 <= response.status_code < 300:
                yield self._error(parser, "http_error", response.status_code)
                return
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "text/event-stream":
                raise ProtocolError("content_type")
            async for frame in frames(response.aiter_bytes(), self._limits):
                if frame.data == "[DONE]":
                    if not isinstance(parser, ChatParser):
                        raise ProtocolError()
                    events = parser.done()
                else:
                    data = json_object(frame.data)
                    if frame.event and frame.event != "message" and frame.event != data.get("type"):
                        raise ProtocolError()
                    events = parser.feed(data)
                for event in events:
                    yield event
                if parser.finished:
                    return
            raise ProtocolError("unexpected_eof", reason="missing_terminal")

    @staticmethod
    def _error(parser: Parser, code: str, status: int | None = None, *, reason: str | None = None) -> ModelEvent:
        return ModelEvent(
            "error",
            error=ModelError(code, ERROR_MESSAGES[code], status, reason),
            usage=parser.usage,
            continuation=parser.snapshot(),
        )
