# Native model protocol module

This is a bounded transport spike for Anthropic Messages, OpenAI Chat
Completions, and OpenAI Responses. It is independent of LingNexus storage,
identity, application containers, and agent execution libraries. It performs one
native streaming request through an injected httpx client. It does not execute
tools, retry requests, persist data, or choose an overall execution library.

## Requirements

- Python 3.12 (uses asyncio.timeout_at and StrEnum).
- pydantic >=2.8,<3 for explicit, frozen ModelProfile and ModelCapabilities.
- httpx >=0.28,<0.29; tested with the parent's installed 0.28.1.
- pytest >=8,<9 for the fixture tests. No provider SDK or async pytest plugin.

Dependency installation and project metadata belong to the parent. This module
does not install anything, look up credentials, or read environment variables.

## Public interface

All public names are exported from lingnexus.models:

    APIFamily
    ModelProfile
    ModelCapabilities
    ModelGateway
    RuntimeCredentials
    StreamLimits
    ModelEvent
    ModelError
    ToolCall
    Usage
    ProviderContinuation

The entry point is:

    ModelGateway(
        client: httpx.AsyncClient,
        *,
        limits: StreamLimits | None = None,
        timeout_seconds: float = 60,
        total_timeout_seconds: float = 300,
    )

    gateway.stream(
        profile: ModelProfile,
        payload: Mapping[str, Any],
        *,
        credentials: RuntimeCredentials,
        effort: str | None = None,
    ) -> AsyncGenerator[ModelEvent, None]

For example, the application can wrap the transport without coupling it to an
application container:

    from contextlib import aclosing
    import httpx
    from lingnexus.models import ModelGateway, ModelProfile, RuntimeCredentials

    async def stream_one_turn(
        profile: ModelProfile, native_body: dict, runtime_api_key: str
    ):
        async with httpx.AsyncClient() as client:
            gateway = ModelGateway(client)
            async with aclosing(gateway.stream(
                profile,
                native_body,
                credentials=RuntimeCredentials(api_key=runtime_api_key),
            )) as events:
                async for event in events:
                    yield event

A shared gateway/client can also serve concurrent calls: parsers, credentials,
and request bodies are per call. The gateway never closes the injected client.
Each request removes inherited authentication headers before applying its runtime
credentials. This includes Authorization, Proxy-Authorization, x-api-key, api-key,
x-goog-api-key, x-auth-token, x-litellm-api-key, x-portkey-api-key, and Cookie.
Client auth is disabled for the request. Each outgoing URL comes only from the
validated profile.endpoint_url; shared client query parameters are never inherited,
including query credentials and provider options. Shared client query parameters,
headers, cookies, and auth settings are not mutated. Other transport and provider
option headers, client event hooks, and the configured transport are retained; the
gateway still applies its per-call timeout.
Use aclosing if iteration can stop early. HTTP responses are closed before a
terminal event is delivered. Cancellation propagates as CancelledError, closes
the response, and does not fabricate a completion or error event.

ModelProfile requires api_family, model, and base_url. api_family accepts:

| Value | Root endpoint | Authentication |
| --- | --- | --- |
| anthropic_messages | /v1/messages | x-api-key; anthropic-version: 2023-06-01 |
| openai_chat_completions | /v1/chat/completions | Authorization: Bearer |
| openai_responses | /v1/responses | Authorization: Bearer |

An origin-only URL gains /v1. Explicit version or proxy prefixes are retained:
https://host/proxy/v1 becomes https://host/proxy/v1/responses for Responses;
https://host/custom becomes https://host/custom/responses. Trailing slashes and
already complete native endpoint paths do not duplicate suffixes. URL userinfo,
queries, and fragments are rejected. Redirects are never followed.

Profiles have no credential field and reject extra fields. RuntimeCredentials
excludes api_key from repr and is supplied only to stream. Never serialize that
runtime object. The gateway does not retain keys on itself or profiles, write
files, or log request/response data. Diagnostic messages contain fixed text, not
provider messages or transport exception strings. The caller owns any client
event hooks or custom HTTP logging and must not log authentication headers.

## Native requests and capabilities

payload is a native provider JSON body, deep-copied before use. Supply messages
and max_tokens for Anthropic; messages for Chat; input or previous_response_id
for Responses. Native tool declarations, results, reasoning objects, signatures,
and unknown request fields pass through. This is intentionally not a universal
message schema or tool-schema translator.

The gateway sets model and stream=true. Conflicting model/stream values are
rejected. Chat uses n=1 and requests stream_options.include_usage=true. The
profile does not guess max_tokens, a model identifier, or feature availability.

ModelCapabilities defaults to streaming=true, tools=true, thinking=false,
encrypted_reasoning=false, effort_levels=(). These are caller declarations,
not a provider capability discovery mechanism:

- streaming=false rejects requests.
- tools=false rejects nonempty tool declarations.
- thinking gates Anthropic enabled/adaptive thinking requests and Responses
  reasoning.summary requests. It does not discard visible material a provider
  actually returns.
- encrypted_reasoning gates requesting include=["reasoning.encrypted_content"].
  Already returned opaque objects can always be supplied for native continuation.
- effort_levels is the explicit allowed set. An empty tuple disables effort
  selection. The gateway validates both the effort argument and effort already
  present in the native request; conflicting values are errors.

| Family | Native effort field |
| --- | --- |
| Anthropic Messages | output_config.effort |
| OpenAI Chat Completions | reasoning_effort |
| OpenAI Responses | reasoning.effort |

Sibling fields, such as reasoning.summary, remain intact. The application must
set appropriate capability flags for the particular native model it chooses.

context_window_tokens, max_output_tokens, input_price_per_million, and
output_price_per_million default to None, explicitly meaning unknown. Prices are
Decimal USD per million tokens. When both prices and input/output counts are known,
the gateway estimates Usage.cost_usd from the immutable call-time profile. Explicit
positive cache reads/writes keep it unknown because cache prices are not configured.
This is an estimate, not a provider bill; service tiers, tool fees and other charges
need their own policy. Raw provider counts/continuation remain untouched.

default_effort is saved only when listed in declared effort_levels. An explicit
call effort overrides that default. temperature/top_p require declared support;
defaults are absent, so models rejecting sampling controls keep working.
cache_system_prompt is an opt-in for Anthropic with prompt_caching support. It marks
only the final system text block ephemeral, never rewrites native continuation and
never asserts a hit. Compatible channels must actually support the request fields.

## Normalized events

ModelEvent is a frozen dataclass with a type discriminator. Unused fields are
None. Raw material and display text are excluded from repr.

| type | Relevant fields and meaning |
| --- | --- |
| text_delta | text, index, item_id when provided; includes visible refusals |
| thinking_delta | returned Anthropic thinking, Responses reasoning text/summary, or Chat reasoning_content text |
| tool_call_delta | tool_call_id, tool_name, index, item_id, arguments_delta |
| tool_call | tool_call with id, name, arguments_json, parsed arguments, optional item_id |
| usage | usage; a cumulative snapshot, never a quantity to sum across events |
| complete | finish_reason, usage, continuation |
| error | error.code/message/status_code, partial usage, partial continuation |

ToolCall.id is the tool invocation ID. Responses item_id and call_id are
different identifiers and remain separate. Fragments are assembled by native
index; parallel calls do not overwrite one another. A completed tool's arguments
must parse as a JSON object. Original argument JSON is retained, including
whitespace. Schema validation and authorization of tool execution belong to the
caller. Wait for the complete event before accepting a turn or executing its
tools: an error may still follow an individually completed tool.

Responses completion reconciles every observed function call with the final output
at its original index. Missing or replaced calls and conflicting identities or
arguments are malformed_stream errors. Arguments finalized by an arguments-done
event or completed item remain fixed even if no argument deltas arrived. Repeated
consistent completions emit one tool_call; conflicting completions or later argument
deltas fail the turn. These checks preserve native unknown fields and signatures.

For Responses display deltas, an omitted item_id is resolved only from an item
already established at that output_index. Missing/unknown indexes, invalid
explicit IDs, and explicit IDs conflicting with the stored item are errors. The
fallback applies to output text, refusals, reasoning summaries, and reasoning text;
it never invents an ID or changes the original event in continuation.events.
Content-part and display-done events retain their existing raw-data handling.

thinking_delta never contains encrypted_content, redacted_thinking.data, or
thinking signatures. Chat's reasoning_content extension is normalized only when
the endpoint actually returns string fragments in that field, and the assembled
field remains in the native message. This extension was reported by the parent's
gateway probe; it is not asserted to be part of OpenAI's standard Chat schema.
Other reasoning extensions remain raw. No hidden reasoning is synthesized or
decoded. Visible summaries are only the material returned by the API.

Usage maps native input/output, total, reasoning, cached-input, and cache-creation
counts when returned; otherwise each field is None. Provider details remain in
Usage.raw. Anthropic usage updates are cumulative, so output_tokens=1 followed by
output_tokens=7 means 7, not 8. A Chat finish_reason does not finish the transport:
it continues through the usage-only chunk until [DONE].
Reported total_tokens is preserved even when it differs from input plus output;
the gateway does not repair accounting. Nested native billing_usage remains in
the raw usage. Reasoning-only turns can legitimately finish with length or
max_tokens and no visible answer text; that is not a malformed stream.

## Continuation and errors

complete and error carry a separate ProviderContinuation with:

- api_family and response_id.
- response: the native final Responses object, or the assembled Anthropic/Chat
  response envelope. On an error, this may be partial.
- events: original decoded provider JSON objects in arrival order. Unknown
  fields/events are preserved, including opaque content and signatures. SSE
  comments and framing fields are transport metadata, not stored provider JSON.

Snapshots are detached from parser state. They are intentionally raw provider
data, not normalized display history. Anthropic continuation uses the assembled
content blocks (including signatures and redacted blocks); Chat uses the assembled
choices[0].message; Responses uses its final output items or response ID according
to the caller's storage/replay policy. Do not replay the entire HTTP response
envelope as an input message. Do not replace raw reasoning items with text
summaries. Unknown delta semantics remain available in events even when this
bounded reducer does not know how to merge them into the response envelope.

Fully consumed streams emit exactly one complete or error. EOF before a native
terminal marker is an error, even if some text arrived. Responses failed and
incomplete outcomes are errors, with native details retained separately. HTTP
error bodies are not read or echoed; only the status is exposed. Error codes are:

    invalid_request, http_error, content_type, transport_error, timeout,
    malformed_stream, unexpected_eof, stream_limit, provider_error,
    incomplete_response, invalid_tool_arguments

SSE supports split byte packets (including split UTF-8 characters), LF/CRLF/CR,
BOM, comments, and multiline data fields. UTF-8 and JSON parsing are strict.
Default bounds are 1 MiB per event, 32 MiB per decoded stream, and 100,000 data
events. httpx operations have a 60-second timeout and each turn has a 300-second
total deadline. Deadline checks apply to gateway reads; the timer never remains
active across a yield into application code. Consumer time counts toward the
deadline, with expiry reported on the next read.

Only text, visible thinking, usage, and modern JSON function tools are normalized.
Provider-hosted tools, audio/images, legacy Chat function_call, and future event
types remain raw native material. There is no SDK abstraction, tool loop, automatic
replay/retry, pricing lookup, or live service validation in this spike.

## Official references read before implementation

- [Anthropic streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [OpenAI Chat streaming events](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
- [OpenAI Chat request and usage options](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [OpenAI Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [OpenAI reasoning and encrypted continuation](https://developers.openai.com/api/docs/guides/reasoning)

Reviewed on 2026-10-08. Fixtures are local synthetic protocol examples, not
recordings with real credentials. Protocol checks use httpx.MockTransport
exclusively at the external HTTP boundary and exercise the real gateway,
framing, and reducers; the historical protocol tests were retired with the
2026-10-10 acceptance layering (recoverable from git history).

Run:

    ./.venv/Scripts/python.exe -m pytest tests/ -q
    ./.venv/Scripts/python.exe -m ruff check src tests scripts
    ./.venv/Scripts/python.exe -m pyright src/lingnexus/models
