"""Offline HTTP fixtures derived from docs.typesafe.ai/api, verified 2026-10-09.

Probabilities are synthetic fixtures, not evidence of Jev accuracy or a live call.
"""

import asyncio
import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError, asdict, replace

import httpx
import pytest

FAKE_KEY = "jev-fixture-not-a-real-credential"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"


@pytest.fixture
def decision():
    name = "lingnexus.activities.decision"
    assert importlib.util.find_spec(name) is not None, "Jev shadow adapter is not implemented"
    return importlib.import_module(name)


@pytest.fixture
def state(decision):
    return decision.ShadowState(
        observation="发布说明新增了中文输入支持。",
        concerns=("关注中文输入支持",),
        previous=("此前仅支持英文输入。",),
        memories=("用户需要中文输入。",),
        conversation_busy=True,
    )


@pytest.fixture
def response_body():
    # The official response envelope and Noul answer shape, with our three ids.
    return {
        "model": MODEL,
        "answers": {
            "relevant": {"type": "noul", "noul": 0.95},
            "new": {"type": "noul", "noul": 0.7},
            "needs_info": {"type": "noul", "noul": 0.2},
        },
        "usage": {"input_tokens": 307, "output_tokens": 20},
    }


def evaluate(decision, state, response_body, *, credentials=FAKE_KEY, **options):
    requests = []

    def handler(request):
        requests.append(request)
        # Preserve invalid JSON numbers as wire bytes so the adapter, rather
        # than httpx's fixture serializer, exercises strict JSON validation.
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(response_body).encode(),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = decision.JevShadowAdapter(client, **options)
            key = None if credentials is None else decision.DecisionCredentials(credentials)
            return await decision.evaluate_shadow(adapter, state, credentials=key)

    return asyncio.run(run()), requests


def test_official_request_and_advisory_only_result(decision, state, response_body):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_body)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://llm-profile.invalid",
            headers={"authorization": "Bearer unrelated-llm-key", "x-api-key": "llm-key"},
            cookies={"profile-session": "unrelated-session"},
            params={"api_key": "unrelated-query-key"},
            auth=httpx.BasicAuth("unrelated", "llm-auth"),
            follow_redirects=True,
            timeout=999,
        ) as client:
            adapter = decision.JevShadowAdapter(client, timeout_seconds=2)
            credentials = decision.DecisionCredentials(FAKE_KEY)
            advice = await decision.evaluate_shadow(adapter, state, credentials=credentials)
            assert not client.is_closed
            assert FAKE_KEY not in repr(credentials)
            assert set(vars(adapter)) == {"_client", "_model", "_timeout"}
            return advice

    advice = asyncio.run(run())
    assert asdict(advice) == {
        "relevant": 0.95,
        "new": 0.7,
        "needs_info": 0.2,
        "model_version": MODEL,
        "question_version": "ling-jev-shadow-v1",
    }
    with pytest.raises(FrozenInstanceError):
        advice.relevant = 1.0
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert "cookie" not in request.headers
    assert "x-api-key" not in request.headers
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 2)
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == MODEL
    assert body["state"] == {
        "observation": state.observation,
        "concerns": list(state.concerns),
        "previous": list(state.previous),
        "memories": list(state.memories),
        "conversation_busy": True,
    }
    assert FAKE_KEY not in request.content.decode()
    assert body["questions"] == {
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


def test_disabled_helper_has_no_side_effects(decision, state):
    assert asyncio.run(decision.evaluate_shadow(None, state)) is None


def test_credentials_are_not_reused_between_calls(decision, state, response_body):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = decision.JevShadowAdapter(client)
            first = await decision.evaluate_shadow(
                adapter, state, credentials=decision.DecisionCredentials(FAKE_KEY)
            )
            assert first is not None
            assert await decision.evaluate_shadow(adapter, state) is None

    asyncio.run(run())
    assert len(requests) == 1


def test_minimal_state_and_text_length_boundary_are_supported(decision, response_body):
    state = decision.ShadowState(observation="x" * 4000)
    advice, requests = evaluate(decision, state, response_body)
    assert advice is not None
    assert json.loads(requests[0].content)["state"]["observation"] == state.observation


@pytest.mark.parametrize(
    "key", [None, "", " 	", "key with spaces", "key\r\nInjected: yes", "密钥", "key\x7f"]
)
def test_unconfigured_or_invalid_credentials_never_call(
    decision, state, response_body, key, monkeypatch
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "environment-is-not-configuration")
    advice, requests = evaluate(decision, state, response_body, credentials=key)
    assert advice is None
    assert requests == []


def test_llm_credentials_are_not_decision_credentials(decision, state):
    from lingnexus.models import RuntimeCredentials

    async def run():
        def handler(request):
            pytest.fail("LLM profile credentials must not configure Jev")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await decision.evaluate_shadow(
                decision.JevShadowAdapter(client),
                state,
                credentials=RuntimeCredentials(api_key=FAKE_KEY),
            )

    assert asyncio.run(run()) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"observation": ""},
        {"observation": " 	"},
        {"observation": b"binary"},
        {"observation": "x" * 4001},
        {"observation": "\ud800"},
        {"concerns": ("x",) * 9},
        {"previous": ("x" * 2001,)},
        {"memories": (None,)},
        {"concerns": "not a sequence of facts"},
        {"conversation_busy": 1},
        {"concerns": ("中" * 2000,) * 8},
    ],
)
def test_invalid_or_oversized_state_is_not_sent(decision, state, response_body, changes):
    advice, requests = evaluate(decision, replace(state, **changes), response_body)
    assert advice is None
    assert requests == []


def test_unsupported_input_shape_is_not_sent(decision, response_body):
    advice, requests = evaluate(decision, {"arbitrary": "context"}, response_body)
    assert advice is None
    assert requests == []


@pytest.mark.parametrize(
    "options",
    [
        {"model": "jev-latest"},
        {"model": "unverified-model"},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": 31},
    ],
)
def test_unsupported_configuration_never_calls(decision, state, response_body, options):
    advice, requests = evaluate(decision, state, response_body, **options)
    assert advice is None
    assert requests == []


@pytest.mark.parametrize(
    "value", [None, True, False, "0.9", -0.01, 1.01, float("nan"), float("inf"), [], {}]
)
def test_probability_requires_finite_number_in_unit_interval(decision, state, response_body, value):
    response_body["answers"]["new"]["noul"] = value
    advice, requests = evaluate(decision, state, response_body)
    assert advice is None
    assert len(requests) == 1


def test_probability_boundaries_are_preserved(decision, state, response_body):
    response_body["answers"]["relevant"]["noul"] = 0
    response_body["answers"]["new"]["noul"] = 1
    advice, _ = evaluate(decision, state, response_body)
    assert advice.relevant == 0.0
    assert advice.new == 1.0
    assert advice.needs_info == 0.2


@pytest.mark.parametrize(
    "defect",
    [
        "missing_model",
        "wrong_model",
        "alias_model",
        "missing_answers",
        "missing_question",
        "extra_question",
        "wrong_type",
        "missing_noul",
        "explanation",
        "confidence",
        "missing_usage",
        "boolean_usage",
        "negative_usage",
        "string_usage",
        "extra_field",
    ],
)
def test_contract_changes_or_partial_results_return_none(decision, state, response_body, defect):
    answers = response_body["answers"]
    if defect.startswith("missing_"):
        field = defect.removeprefix("missing_")
        if field == "question":
            answers.pop("needs_info")
        elif field == "noul":
            answers["new"].pop("noul")
        else:
            response_body.pop(field)
    elif defect == "wrong_model":
        response_body["model"] = "jev-99.0.0"
    elif defect == "alias_model":
        response_body["model"] = "jev-latest"
    elif defect == "extra_question":
        answers["act_now"] = {"type": "noul", "noul": 1}
    elif defect == "wrong_type":
        answers["new"]["type"] = "choice"
    elif defect == "explanation":
        answers["new"]["explanation"] = FAKE_KEY
    elif defect == "confidence":
        answers["new"]["confidence"] = 0.9
    elif defect == "boolean_usage":
        response_body["usage"]["input_tokens"] = True
    elif defect == "negative_usage":
        response_body["usage"]["output_tokens"] = -1
    elif defect == "string_usage":
        response_body["usage"]["input_tokens"] = "307"
    elif defect == "extra_field":
        response_body["action"] = "notify"
    advice, _ = evaluate(decision, state, response_body)
    assert advice is None


@pytest.mark.parametrize("status", [204, 301, 307, 401, 403, 404, 422, 429, 500, 529])
def test_http_failure_or_redirect_is_not_retried(decision, state, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://other.invalid"}, text=FAKE_KEY)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await decision.evaluate_shadow(
                decision.JevShadowAdapter(client),
                state,
                credentials=decision.DecisionCredentials(FAKE_KEY),
            )

    assert asyncio.run(run()) is None
    assert len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, OSError])
def test_transport_errors_never_expose_provider_text(decision, state, error, caplog, capsys):
    def handler(request):
        raise error(FAKE_KEY)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await decision.evaluate_shadow(
                decision.JevShadowAdapter(client),
                state,
                credentials=decision.DecisionCredentials(FAKE_KEY),
            )

    assert asyncio.run(run()) is None
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("error", [RuntimeError, ValueError, TypeError, AssertionError])
def test_unexpected_transport_programming_errors_propagate(decision, state, error):
    def handler(request):
        raise error("fixture programming defect")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await decision.evaluate_shadow(
                decision.JevShadowAdapter(client),
                state,
                credentials=decision.DecisionCredentials(FAKE_KEY),
            )

    with pytest.raises(error, match="fixture programming defect"):
        asyncio.run(run())


def test_closed_client_is_unavailable_without_a_call(decision, state):
    def handler(request):
        pytest.fail("Closed client must not send a request")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client.aclose()
        return await decision.evaluate_shadow(
            decision.JevShadowAdapter(client),
            state,
            credentials=decision.DecisionCredentials(FAKE_KEY),
        )

    assert asyncio.run(run()) is None


class Packets(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed, self.read = chunks, delay, False, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def exchange(decision, state, stream, content_type="application/json", timeout=2):
    def handler(request):
        return httpx.Response(200, headers={"content-type": content_type}, stream=stream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await decision.evaluate_shadow(
                decision.JevShadowAdapter(client, timeout_seconds=timeout),
                state,
                credentials=decision.DecisionCredentials(FAKE_KEY),
            )
            assert not client.is_closed
            return result

    return asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"null",
        b'{"answers": {}, "answers": {}}',
        b'{"value": NaN}',
        b"\xff",
        b"[" * 2000,
    ],
)
def test_invalid_json_is_rejected_and_response_closed(decision, state, body):
    stream = Packets([body])
    assert exchange(decision, state, stream) is None
    assert stream.closed


def test_non_json_response_is_not_consumed(decision, state):
    stream = Packets([FAKE_KEY.encode()])
    assert exchange(decision, state, stream, content_type="text/event-stream") is None
    assert stream.closed
    assert stream.read == 0


def test_response_limit_stops_reading(decision, state):
    stream = Packets([b"x" * 8192] * 4)
    assert exchange(decision, state, stream) is None
    assert stream.closed
    assert stream.read == 3


def test_response_at_byte_limit_is_supported(decision, state, response_body):
    body = json.dumps(response_body).encode()
    stream = Packets([body + b" " * (16_384 - len(body))])
    assert exchange(decision, state, stream) is not None
    assert stream.closed


def test_chunked_valid_json_and_charset(decision, state, response_body):
    body = json.dumps(response_body).encode()
    stream = Packets([body[:30], body[30:]])
    advice = exchange(decision, state, stream, content_type="Application/JSON; charset=utf-8")
    assert advice.model_version == MODEL
    assert stream.closed


def test_total_deadline_closes_slow_response(decision, state, response_body):
    body = json.dumps(response_body).encode()
    stream = Packets([bytes([byte]) for byte in body], delay=0.005)
    assert exchange(decision, state, stream, timeout=0.02) is None
    assert stream.closed
    assert stream.read < len(body)


def test_deadline_also_bounds_waiting_for_headers(decision, state):
    async def handler(request):
        await asyncio.sleep(1)
        pytest.fail("The total timeout should cancel the header wait")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await decision.evaluate_shadow(
                decision.JevShadowAdapter(client, timeout_seconds=0.01),
                state,
                credentials=decision.DecisionCredentials(FAKE_KEY),
            )

    assert asyncio.run(run()) is None


def test_external_cancellation_propagates_and_closes_response(decision, state):
    started = asyncio.Event()

    class Waiting(Packets):
        async def __aiter__(self):
            started.set()
            await asyncio.Future()
            yield b"unreachable"

    stream = Waiting([])

    async def run():
        def handler(request):
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(
                decision.evaluate_shadow(
                    decision.JevShadowAdapter(client),
                    state,
                    credentials=decision.DecisionCredentials(FAKE_KEY),
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not client.is_closed

    asyncio.run(run())
    assert stream.closed
