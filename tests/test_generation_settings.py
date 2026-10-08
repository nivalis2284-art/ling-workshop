"""Generation settings use declared capabilities and fixture HTTP only."""

import asyncio
import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from lingnexus.common.errors import PermissionDenied
from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials
from lingnexus.models._sse import ProtocolError
from lingnexus.models.gateway import _request_body

FAMILIES = (
    "anthropic_messages",
    "openai_chat_completions",
    "openai_responses",
)


def profile(family="openai_chat_completions", **settings):
    return ModelProfile(
        api_family=family,
        model="fixture-reasoning-model",
        base_url="https://generation.invalid",
        **settings,
    )


def payload(family):
    if family == "openai_responses":
        return {"input": [], "store": False}
    return {"messages": [], "max_tokens": 128}


def native_effort(body, family, value):
    if family == "openai_chat_completions":
        body["reasoning_effort"] = value
    else:
        field = "reasoning" if family == "openai_responses" else "output_config"
        body[field] = {"effort": value, "native_extension": {"keep": True}}


def read_effort(body, family):
    if family == "openai_chat_completions":
        return body.get("reasoning_effort")
    field = "reasoning" if family == "openai_responses" else "output_config"
    return body.get(field, {}).get("effort")


def test_settings_and_prices_round_trip_without_copying_runtime_credentials(app, app_factory):
    configured = profile(
        "anthropic_messages",
        capabilities={
            "effort_levels": ["low", "high"],
            "temperature": True,
            "top_p": True,
            "prompt_caching": True,
        },
        default_effort="low",
        temperature=0.4,
        top_p=0.9,
        cache_system_prompt=True,
        input_price_per_million=Decimal("2.50"),
        output_price_per_million=Decimal(10),
    )
    original = app.profiles.save("原预设", configured, api_key="fixture-original-only")
    clone = app.profiles.save("新预设", configured)
    assert clone["id"] != original["id"]
    assert clone["credentials_available"] is False
    with pytest.raises(PermissionDenied):
        app.profiles.credentials(clone["id"])
    app.profiles.save(
        "新预设", configured, profile_id=clone["id"], api_key="fixture-separately-submitted"
    )
    assert app.profiles.credentials(original["id"]).api_key == "fixture-original-only"
    with app.database.transaction() as db:
        assert "fixture-original-only" not in str(list(db.iterdump()))
        assert "fixture-separately-submitted" not in str(list(db.iterdump()))
    reopened = app_factory(app.database.path)
    try:
        saved = reopened.profiles.get(clone["id"])
        assert ModelProfile.model_validate(saved["profile"]) == configured
        assert saved["credentials_available"] is False
    finally:
        reopened.close()


@pytest.mark.parametrize("family", FAMILIES)
def test_legacy_profile_has_no_optional_generation_fields_or_guessed_support(family):
    old = profile(family)
    assert old.default_effort is None
    assert old.temperature is None and old.top_p is None
    assert old.cache_system_prompt is False
    assert not old.capabilities.temperature and not old.capabilities.top_p
    assert not old.capabilities.prompt_caching
    assert old.input_price_per_million is None and old.output_price_per_million is None
    body = _request_body(old, payload(family), None)
    assert read_effort(body, family) is None
    assert "temperature" not in body and "top_p" not in body
    assert "cache_control" not in body


@pytest.mark.parametrize(
    "settings",
    [
        {"default_effort": "high"},
        {"default_effort": "max", "capabilities": {"effort_levels": ["low", "high"]}},
        {"temperature": 0.5},
        {"top_p": 0.9},
        {"cache_system_prompt": True},
        {"cache_system_prompt": True, "capabilities": {"prompt_caching": True}},
    ],
)
def test_invalid_defaults_or_unsupported_profile_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        profile(**settings)


@pytest.mark.parametrize(
    "field,values",
    [
        ("temperature", [-0.01, 2.01, True, "0.5", float("nan"), float("inf")]),
        ("top_p", [-0.01, 1.01, False, "0.9", float("nan"), float("inf")]),
    ],
)
def test_sampling_values_require_finite_numbers_within_bounds(field, values):
    for value in values:
        with pytest.raises(ValidationError):
            profile(capabilities={field: True}, **{field: value})


def test_anthropic_temperature_uses_its_native_upper_bound():
    with pytest.raises(ValidationError):
        profile("anthropic_messages", capabilities={"temperature": True}, temperature=1.01)
    assert profile(capabilities={"temperature": True}, temperature=2).temperature == 2


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("source", ["default", "argument", "native"])
def test_effort_default_and_explicit_overrides_use_only_the_native_field(family, source):
    configured = profile(
        family, capabilities={"effort_levels": ["low", "high"]}, default_effort="low"
    )
    request = payload(family)
    if source == "native":
        native_effort(request, family, "high")
    before = deepcopy(request)
    body = _request_body(configured, request, "high" if source == "argument" else None)
    assert read_effort(body, family) == ("low" if source == "default" else "high")
    if source == "native" and family != "openai_chat_completions":
        field = "reasoning" if family == "openai_responses" else "output_config"
        assert body[field]["native_extension"] == {"keep": True}
    assert request == before


@pytest.mark.parametrize("family", FAMILIES)
def test_conflicting_explicit_effort_is_rejected(family):
    configured = profile(
        family, capabilities={"effort_levels": ["low", "high"]}, default_effort="low"
    )
    request = payload(family)
    native_effort(request, family, "low")
    with pytest.raises(ProtocolError):
        _request_body(configured, request, "high")


@pytest.mark.parametrize(
    "family,other_family",
    [(family, other) for family in FAMILIES for other in FAMILIES if family != other],
)
def test_effort_cannot_bypass_validation_in_another_familys_field(family, other_family):
    request = payload(family)
    native_effort(request, other_family, "high")
    with pytest.raises(ProtocolError):
        _request_body(profile(family), request, None)


@pytest.mark.parametrize("family", FAMILIES)
def test_declared_sampling_defaults_are_native_and_caller_conflicts_are_rejected(family):
    configured = profile(
        family, capabilities={"temperature": True, "top_p": True}, temperature=0, top_p=1
    )
    request = payload(family)
    body = _request_body(configured, request, None)
    assert body["temperature"] == 0 and body["top_p"] == 1
    for field, value in (("temperature", 0), ("top_p", 1)):
        assert _request_body(configured, {**request, field: value}, None)[field] == value
        with pytest.raises(ProtocolError):
            _request_body(configured, {**request, field: 0.5}, None)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("field", ["temperature", "top_p"])
def test_native_sampling_is_gated_even_without_a_profile_default(family, field):
    request = {**payload(family), field: 0.5}
    with pytest.raises(ProtocolError):
        _request_body(profile(family), request, None)
    declared = profile(family, capabilities={field: True})
    assert _request_body(declared, request, None)[field] == 0.5
    for invalid in (True, "0.5", None, -1, 3, float("nan"), float("inf")):
        with pytest.raises(ProtocolError):
            _request_body(declared, {**request, field: invalid}, None)


@pytest.mark.parametrize(
    "system",
    [
        "stable instructions",
        [
            {"type": "text", "text": "first", "native_extension": {"keep": True}},
            {"type": "text", "text": "last"},
        ],
    ],
)
def test_anthropic_cache_marks_only_the_last_system_block_and_preserves_native_material(system):
    configured = profile(
        "anthropic_messages", capabilities={"prompt_caching": True}, cache_system_prompt=True
    )
    request = {
        **payload("anthropic_messages"),
        "system": system,
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "visible", "signature": "opaque"}],
            }
        ],
    }
    before = deepcopy(request)
    body = _request_body(configured, request, None)
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    if isinstance(system, list):
        assert body["system"][0] == system[0]
    else:
        assert body["system"][-1]["text"] == system
    assert body["messages"] == request["messages"]
    assert "cache_control" not in body
    assert request == before


def test_cache_capability_alone_does_not_enable_caching_and_empty_system_is_not_invented():
    configured = profile("anthropic_messages", capabilities={"prompt_caching": True})
    request = {**payload("anthropic_messages"), "system": "stable"}
    assert _request_body(configured, request, None)["system"] == "stable"
    enabled = profile(
        "anthropic_messages", capabilities={"prompt_caching": True}, cache_system_prompt=True
    )
    for system in (None, "", []):
        request = payload("anthropic_messages")
        if system is not None:
            request["system"] = system
        body = _request_body(enabled, request, None)
        assert body.get("system") == system


def test_cache_rejects_a_conflicting_breakpoint_instead_of_overwriting_it():
    configured = profile(
        "anthropic_messages", capabilities={"prompt_caching": True}, cache_system_prompt=True
    )
    request = {
        **payload("anthropic_messages"),
        "system": [
            {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ],
    }
    before = deepcopy(request)
    with pytest.raises(ProtocolError):
        _request_body(configured, request, None)
    assert request == before


@pytest.mark.parametrize("field", ["system", "tools", "messages", "cache_control"])
def test_caller_native_cache_markers_require_declared_support(field):
    request = payload("anthropic_messages")
    block = {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}
    request[field] = (
        [{"role": "user", "content": [block]}]
        if field == "messages"
        else block["cache_control"]
        if field == "cache_control"
        else [block]
    )
    before = deepcopy(request)
    with pytest.raises(ProtocolError):
        _request_body(profile("anthropic_messages"), request, None)
    declared = profile("anthropic_messages", capabilities={"prompt_caching": True})
    assert _request_body(declared, request, None)[field] == request[field]
    assert request == before


def test_gateway_cache_opt_in_keeps_headers_raw_continuation_and_reported_usage():
    requests = []
    native = {
        "type": "message_start",
        "message": {
            "id": "fixture",
            "type": "message",
            "role": "assistant",
            "model": "fixture",
            "content": [],
            "usage": {"input_tokens": 7, "output_tokens": 0},
            "native_extension": {"signature": "opaque"},
        },
    }
    wire_events = [
        native,
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    wire = "".join("data: " + json.dumps(e) + "\n\n" for e in wire_events).encode()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            configured = profile(
                "anthropic_messages",
                capabilities={"prompt_caching": True},
                cache_system_prompt=True,
            )
            return [
                event
                async for event in ModelGateway(client).stream(
                    configured,
                    {**payload("anthropic_messages"), "system": "stable"},
                    credentials=RuntimeCredentials("fixture-not-a-live-key"),
                )
            ]

    events = asyncio.run(run())
    assert events[-1].type == "complete"
    assert events[-1].usage.cached_input_tokens is None
    assert events[-1].usage.cache_creation_input_tokens is None
    assert events[-1].usage.cost_usd is None
    assert events[-1].continuation.events == tuple(wire_events)
    assert events[-1].continuation.response["native_extension"] == {"signature": "opaque"}
    assert requests[0].headers["anthropic-version"] == "2023-06-01"
    assert "anthropic-beta" not in requests[0].headers
    assert json.loads(requests[0].content)["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_unsupported_caller_sampling_fails_before_any_http_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                event
                async for event in ModelGateway(client).stream(
                    profile(),
                    {**payload("openai_chat_completions"), "temperature": 0.5},
                    credentials=RuntimeCredentials("fixture-not-a-live-key"),
                )
            ]

    events = asyncio.run(run())
    assert not requests
    assert events[-1].error.code == "invalid_request"


def priced_stream(family, prices, raw_usage, *, error=False):
    if family == "anthropic_messages":
        native = [
            {
                "type": "message_start",
                "message": {"id": "priced", "content": [], "usage": raw_usage},
            },
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": raw_usage},
        ]
        terminal = {"type": "message_stop"}
    elif family == "openai_chat_completions":
        native = [
            {
                "id": "priced",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": raw_usage,
            }
        ]
        terminal = "[DONE]"
    else:
        native = [
            {
                "type": "response.created",
                "response": {"id": "priced", "output": [], "usage": raw_usage},
            }
        ]
        terminal = {
            "type": "response.completed",
            "response": {"id": "priced", "status": "completed", "output": [], "usage": raw_usage},
        }
    if not error:
        native.append(terminal)
    wire = "".join(
        "data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in native
    ).encode()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=wire
                )
            )
        ) as client:
            return [
                event
                async for event in ModelGateway(client).stream(
                    profile(family, **prices),
                    payload(family),
                    credentials=RuntimeCredentials("fixture-not-a-live-key"),
                )
            ]

    return asyncio.run(run()), native


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("cache", [None, 0])
def test_call_time_prices_apply_to_every_usage_snapshot_including_complete_and_error(
    family, error, cache
):
    chat = family == "openai_chat_completions"
    raw = {
        "prompt_tokens" if chat else "input_tokens": 10,
        "completion_tokens" if chat else "output_tokens": 4,
        "total_tokens": 999,
        "native_extension": {"keep": True},
    }
    if cache is not None:
        raw.update(cache_read_input_tokens=cache, cache_creation_input_tokens=cache)
    events, native = priced_stream(
        family,
        {"input_price_per_million": Decimal("2.5"), "output_price_per_million": Decimal(10)},
        raw,
        error=error,
    )
    assert events[-1].type == ("error" if error else "complete")
    if error:
        assert events[-1].error.code == "unexpected_eof"
    usages = [e.usage for e in events if e.usage is not None]
    assert len(usages) >= 2
    assert all(u.cost_usd == Decimal("0.000065") for u in usages)
    assert all(u.raw == raw for u in usages)
    assert events[-1].continuation.events == tuple(e for e in native if isinstance(e, dict))


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "missing",
    ["input_price", "output_price", "input_tokens", "output_tokens", "cache_read", "cache_write"],
)
def test_cost_is_unknown_without_both_prices_counts_or_with_reported_cache_tokens(family, missing):
    chat = family == "openai_chat_completions"
    input_field = "prompt_tokens" if chat else "input_tokens"
    output_field = "completion_tokens" if chat else "output_tokens"
    raw = {input_field: 10, output_field: 4}
    prices = {"input_price_per_million": Decimal("2.5"), "output_price_per_million": Decimal(10)}
    if missing.endswith("price"):
        prices.pop(
            "input_price_per_million" if missing == "input_price" else "output_price_per_million"
        )
    elif missing.endswith("tokens"):
        raw.pop(input_field if missing == "input_tokens" else output_field)
    else:
        raw[
            "cache_read_input_tokens" if missing == "cache_read" else "cache_creation_input_tokens"
        ] = 1
    events, _ = priced_stream(family, prices, raw)
    assert events[-1].type == "complete"
    assert all(e.usage.cost_usd is None for e in events if e.usage is not None)


def test_explicit_zero_prices_and_counts_are_known_zero_cost():
    events, _ = priced_stream(
        "openai_chat_completions",
        {"input_price_per_million": Decimal(0), "output_price_per_million": Decimal(0)},
        {"prompt_tokens": 0, "completion_tokens": 0},
    )
    assert events[-1].usage.cost_usd == Decimal(0)
