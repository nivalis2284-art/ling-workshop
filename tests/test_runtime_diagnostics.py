"""Regression coverage for the bounded structural diagnostics boundary.

Only fixed identifiers, package-local source locations and a whitelisted
exception type name may cross this boundary. Native exception text, external
paths, custom type names and provider material must never reach events.
"""

import asyncio
import json

import httpx
import pytest


def _sse(*events):
    return "".join("data: " + json.dumps(item) + "\n\n" for item in events) + "data: [DONE]\n\n"


def test_diagnostic_keeps_only_whitelisted_structure(tmp_path):
    from lingnexus.common.diagnostics import diagnostic

    secret = "sk-live-diagnostic-secret-123"
    external = tmp_path / "private" / "credential-notes.txt"

    class ProviderFailure(Exception):
        pass

    def fail():
        raise ProviderFailure(f"token {secret} while reading {external}")

    with pytest.raises(ProviderFailure) as caught:
        fail()

    detail = diagnostic("model", caught.value)
    assert set(detail) == {"diagnostic_id", "stage", "exception_type", "frames"}
    assert detail["stage"] == "model"
    # A custom exception type name is not evidence: it collapses to a fixed name.
    assert detail["exception_type"] == "Exception"
    # Frames outside the installed package are dropped, so no test or user path leaks.
    assert detail["frames"] == []
    encoded = json.dumps(detail, ensure_ascii=False)
    assert secret not in encoded
    assert "credential-notes" not in encoded
    assert "ProviderFailure" not in encoded
    assert "while reading" not in encoded
    assert str(tmp_path) not in encoded
    # Reason and stage are fixed vocabularies; anything else is dropped or normalized.
    assert "reason" not in diagnostic("model", reason="not_whitelisted")
    assert diagnostic("model", reason="response_id_changed")["reason"] == "response_id_changed"
    assert diagnostic("not-a-stage")["stage"] == "initialization"


def test_gateway_response_id_change_carries_reason_without_releasing_tools():
    from lingnexus.models.gateway import ModelGateway
    from lingnexus.models.types import APIFamily, ModelProfile, RuntimeCredentials

    stream = _sse(
        {"id": "native-1", "choices": [{"index": 0, "delta": {
            "role": "assistant",
            "tool_calls": [{"index": 0, "id": "pending-write", "type": "function",
                            "function": {"name": "create_file",
                                         "arguments": json.dumps({"path": "x.txt", "text": "y"})}}],
        }, "finish_reason": None}]},
        {"id": "native-2", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )

    def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ModelGateway(client)
            profile = ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model="fixture",
                base_url="https://fixture.test",
            )
            return [event async for event in gateway.stream(
                profile,
                {"messages": [{"role": "user", "content": "hi"}]},
                credentials=RuntimeCredentials(api_key="fixture-key"),
            )]

    events = asyncio.run(scenario())
    errors = [event for event in events if event.type == "error"]
    assert len(errors) == 1, events
    error = errors[0]
    assert error.error.code == "malformed_stream"
    assert error.error.reason == "response_id_changed"
    # The buffered tool call was never finalized into a releasable tool_call event.
    assert all(event.type != "tool_call" for event in events)


def test_task_runtime_model_exception_records_sanitized_diagnostic(app, agent_id, tmp_path):
    from lingnexus.models import APIFamily, ModelProfile
    from lingnexus.tasks import TaskRuntime

    secret = "sk-live-task-secret-456"
    external = tmp_path / "outside" / "notes.txt"

    class ProviderFailure(Exception):
        pass

    def handler(_request):
        raise ProviderFailure(f"provider failed with {secret} at {external}")

    profile = app.profiles.save(
        "diagnostic-task",
        ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                     model="offline-diagnostics", base_url="https://fixture.invalid/v1"),
        api_key="fixture-only",
    )
    app.profiles.select(profile["id"], None)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = TaskRuntime(app, client)
        try:
            tid = await runtime.submit({
                "agent_id": agent_id, "objective": "Trigger a model failure",
                "request_id": "diagnostic-task-failure",
            })
            for _ in range(500):
                task = runtime.get(tid)
                if task["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert task["status"] == "blocked", task
            assert task["error_code"] == "task_runtime_error"
            return tid, runtime.events(tid)
        finally:
            await runtime.aclose()

    _tid, events = asyncio.run(scenario())
    diagnostics = [item for item in events if item["type"] == "diagnostic"]
    assert len(diagnostics) == 1, events
    detail = diagnostics[0]["data"]
    assert detail["stage"] == "model"
    assert detail["exception_type"] == "Exception"
    assert detail["frames"], "a real runtime failure must keep local source locations"
    assert all(":" not in frame["file"] for frame in detail["frames"])
    encoded = json.dumps(events, ensure_ascii=False)
    assert secret not in encoded
    assert "ProviderFailure" not in encoded
    assert str(tmp_path) not in encoded
    assert "outside" not in encoded
