"""Optional Jev shadow evaluation, independent of the main attention path.

The official HTTP contract was read with Python urllib on 2026-10-09:
https://docs.typesafe.ai/api and https://docs.typesafe.ai/models.
POST https://api.typesafe.ai/v1/systemone uses Bearer authentication and
{state, model, questions}; Noul answers are {type: "noul", noul: probability}.
This module pins the documented jev-1.13.0 contract. No live inference was used
to verify it; the tests use synthetic HTTP fixtures, not accuracy measurements.

Exact use (inside an async function, with a caller-owned httpx.AsyncClient)::

    adapter = JevShadowAdapter(client)
    state = ShadowState(
        observation="The release now supports Chinese input.",
        concerns=("Chinese input support",),
        previous=("The previous release supported English only.",),
        memories=("The user needs Chinese input.",),
    )
    advice = await evaluate_shadow(
        adapter, state, credentials=DecisionCredentials(runtime_jev_key)
    )

Pass adapter=None or omit credentials to disable evaluation without a call.
DecisionCredentials is an explicit, caller-owned in-memory value, separate from
LLM profiles. The adapter/helper never retain credentials, read environment
keys, use profile credentials, write files/database records, or log responses
or exceptions. The injected client remains open; its transport/hooks belong to
the caller. Each configured evaluation makes at most one request, without
retries, redirects, inherited headers/cookies/query parameters/client auth.

State contains only supplied facts: a nonblank observation of <=4000 characters,
and up to eight nonblank strings of <=2000 characters in each context tuple.
Its compact UTF-8 JSON must fit 16384 bytes. Oversized/unsupported inputs are
rejected, not silently truncated. Responses must fit 16384 decoded bytes and
match the exact documented envelope and all three Noul answers. Both transport
I/O and the entire exchange use timeout_seconds (default 5, range (0, 30]).
Unconfigured/unsupported values, invalid JSON, and expected HTTP/OS/timeout
failures return None. Unexpected programming errors and external asyncio
cancellation propagate. A different model version requires a reviewed contract
and fixtures before acceptance.

ShadowAdvice contains only three yes probabilities and model/question versions.
There is no invented explanation, independent confidence, threshold, action,
permission, routing decision, or persistence. Questions are evaluated
independently; `new` compares only the supplied previous facts, and `needs_info`
is another fallible estimate, not guaranteed uncertainty detection. Probabilities
are not guarantees of task accuracy. Chinese performance, missed changes, and
adversarial observations need held-out evaluation before any operational use.
Arithmetic, date comparisons, permissions, deduplication, and action decisions
stay in code/the main agent. This helper is advisory only: never authority or
the sole attention entry, and must not suppress reminders, reviews, exploration,
or the existing attention flow. The parent may wire an optional shell later.
"""

import asyncio
import json
import math
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import httpx

JEV_MODEL = "jev-1.13.0"
QUESTION_VERSION = "ling-jev-shadow-v1"
_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MAX_STATE_BYTES = 16_384
_MAX_RESPONSE_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class DecisionCredentials:
    """Explicit per-call Jev credential; never persist or reuse an LLM profile key."""

    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ShadowState:
    """Small factual snapshot; context fields must be tuples, not raw histories."""

    observation: str
    concerns: tuple[str, ...] = ()
    previous: tuple[str, ...] = ()
    memories: tuple[str, ...] = ()
    conversation_busy: bool = False


@dataclass(frozen=True, slots=True)
class ShadowAdvice:
    """Fallible yes probabilities, without thresholds, explanations, or authority."""

    relevant: float
    new: float
    needs_info: float
    model_version: str
    question_version: str = QUESTION_VERSION


def _questions() -> dict[str, dict[str, Any]]:
    # Fresh fixed questions per call: observation text cannot extend the rubric.
    return {
        "relevant": {
            "type": "noul",
            "instructions": "Does `observation` relate to any item in `concerns`?",
            "criteria": {
                "true": "The observation concerns at least one listed concern.",
                "false": "No listed concern relates to the observation, or concerns is empty.",
            },
        },
        "new": {
            "type": "noul",
            "instructions": "Does `observation` add a substantive fact absent from `previous`?",
            "criteria": {
                "true": "At least one substantive fact is absent from the supplied previous facts.",
                "false": "The observation only repeats or rephrases the supplied previous facts.",
            },
        },
        "needs_info": {
            "type": "noul",
            "instructions": "Is information missing to assess how `observation` affects `concerns`?",
            "criteria": {
                "true": "The supplied facts omit information needed to assess the effect.",
                "false": "The supplied facts suffice to assess the effect.",
            },
        },
    }


def _state_payload(state: ShadowState) -> dict[str, Any] | None:
    def text(value: Any, limit: int) -> bool:
        return type(value) is str and 0 < len(value) <= limit and bool(value.strip())

    if type(state) is not ShadowState or not text(state.observation, 4000):
        return None
    if type(state.conversation_busy) is not bool:
        return None
    for items in (state.concerns, state.previous, state.memories):
        if type(items) is not tuple or len(items) > 8 or any(not text(s, 2000) for s in items):
            return None
    payload = {
        "observation": state.observation,
        "concerns": list(state.concerns),
        "previous": list(state.previous),
        "memories": list(state.memories),
        "conversation_busy": state.conversation_busy,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return payload if len(encoded) <= _MAX_STATE_BYTES else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON field")
        obj[key] = value
    return obj


def _invalid_constant(value: str) -> Any:
    raise ValueError("Non-finite JSON constant")


def _advice(body: bytes) -> ShadowAdvice | None:
    try:
        data = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (ValueError, RecursionError):
        # Invalid UTF-8, malformed/duplicate JSON, non-finite literals, excessive
        # integer digits, and nesting are untrusted response-contract failures.
        return None
    if type(data) is not dict or set(data) != {"model", "answers", "usage"}:
        return None
    if data["model"] != JEV_MODEL:
        return None
    usage = data["usage"]
    if type(usage) is not dict or set(usage) != {"input_tokens", "output_tokens"}:
        return None
    if any(type(value) is not int or value < 0 for value in usage.values()):
        return None
    answers = data["answers"]
    if type(answers) is not dict or set(answers) != {"relevant", "new", "needs_info"}:
        return None
    probabilities: dict[str, float] = {}
    for name, answer in answers.items():
        if type(answer) is not dict or set(answer) != {"type", "noul"}:
            return None
        value = answer["noul"]
        if (
            answer["type"] != "noul"
            or type(value) not in (int, float)
            or not 0 <= value <= 1
            or not math.isfinite(value)
        ):
            return None
        probabilities[name] = float(value)
    return ShadowAdvice(
        relevant=probabilities["relevant"],
        new=probabilities["new"],
        needs_info=probabilities["needs_info"],
        model_version=data["model"],
    )


class JevShadowAdapter:
    """One bounded official request using an injected, caller-owned async client.

    Only model=jev-1.13.0 and finite timeout_seconds in (0, 30] are supported.
    Unsupported configuration returns None on evaluation, without network I/O.
    No credentials are kept on this object; supply DecisionCredentials per call.
    """

    def __init__(
        self, client: httpx.AsyncClient, *, model: str = JEV_MODEL, timeout_seconds: float = 5
    ) -> None:
        self._client, self._model, self._timeout = client, model, timeout_seconds

    async def evaluate(
        self, state: ShadowState, *, credentials: DecisionCredentials | None = None
    ) -> ShadowAdvice | None:
        """Return advice/None; propagate programming errors and external cancellation."""
        if type(credentials) is not DecisionCredentials:
            return None
        key = credentials.api_key
        if (
            type(key) is not str
            or not key
            or not key.isascii()
            or any(not 33 <= ord(char) <= 126 for char in key)
            or self._model != JEV_MODEL
            or type(self._timeout) not in (int, float)
            or not 0 < self._timeout <= 30
            or self._client.is_closed
        ):
            return None
        try:
            state_payload = _state_payload(state)
        except UnicodeError:
            return None
        if state_payload is None:
            return None
        # Direct Request avoids all merged client defaults, including cookies
        # and query keys. send(auth=None) also disables the client's auth flow.
        request = httpx.Request(
            "POST",
            _ENDPOINT,
            headers={
                "authorization": f"Bearer {key}",
                "accept": "application/json",
                "content-type": "application/json",
            },
            json={"state": state_payload, "model": self._model, "questions": _questions()},
            extensions={"timeout": httpx.Timeout(self._timeout).as_dict()},
        )
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
                async with aclosing(response):
                    if response.status_code != 200:
                        return None
                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if media_type.strip().lower() != "application/json":
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            return None
                        body.extend(chunk)
                    return _advice(bytes(body))
        except (httpx.HTTPError, OSError, TimeoutError):
            # Known I/O failures yield no advice. Never reflect provider/transport
            # exception text. Programming defects and cancellation remain visible.
            return None


async def evaluate_shadow(
    adapter: JevShadowAdapter | None,
    state: ShadowState,
    *,
    credentials: DecisionCredentials | None = None,
) -> ShadowAdvice | None:
    """Independent opt-in helper; stores no keys/results and never routes attention."""
    if not isinstance(adapter, JevShadowAdapter):
        return None
    return await adapter.evaluate(state, credentials=credentials)
