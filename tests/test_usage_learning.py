import asyncio
import json

import httpx

from lingnexus.learning import ConsolidationWorker
from lingnexus.models import APIFamily, ModelProfile


def test_usage_includes_learning_and_does_not_count_repeated_snapshots(app, agent_id):
    model = app.profiles.save(
        "辅助",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="test",
            base_url="https://example.test",
        ),
        api_key="fixture-key",
    )
    app.profiles.select(model["id"], None)
    app.history.add(agent_id, "有价值记录", kind="user", scope="real", request_id="source")

    def provider(request):
        result = {"event": None, "knowledge": [], "changes": []}
        frames = [
            {
                "id": "r",
                "choices": [
                    {"index": 0, "delta": {"content": json.dumps(result)}, "finish_reason": None}
                ],
            },
            {
                "id": "r",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 20},
            },
        ]
        body = (
            "".join("data: " + json.dumps(v) + chr(10) * 2 for v in frames)
            + "data: [DONE]"
            + chr(10) * 2
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            worker = ConsolidationWorker(app, client=client)
            assert (await worker.run_once(agent_id))["status"] == "skipped"

    asyncio.run(execute())
    from lingnexus.observability.usage import UsageService

    summary = UsageService(app.database).summary(agent_id)
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 10
    assert summary["output_tokens"] == 5
    assert summary["provider_total_tokens"] == 20
    assert summary["categories"]["learning"] == 1
