import asyncio

import httpx

from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime


def test_usage_counts_only_latest_snapshot_per_run_and_marks_cost_unknown(app, agent_id):
    from lingnexus.observability.usage import UsageService
    profile = app.profiles.save("测试", ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="test", base_url="https://example.test"), api_key="fixture-usage-key")
    app.profiles.select(profile["id"], None)

    def response(request):
        import json
        chunks = [
            {"id":"c", "choices":[{"index":0, "delta":{"content":"完成"},"finish_reason":None}], "usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}},
            {"id":"c", "choices":[{"index":0,"delta":{},"finish_reason":"stop"}], "usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":20}},
        ]
        body = "".join("data: " + json.dumps(c) + chr(10)*2 for c in chunks) + "data: [DONE]" + chr(10)*2
        return httpx.Response(200, headers={"Content-Type":"text/event-stream"}, content=body.encode())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            runtime = ChatRuntime(app, client=client)
            run = runtime.submit(ChatRequest(agent_id=agent_id, message="测试", request_id="usage"))
            events = [event async for event in runtime.events(run["id"])]
            assert events[-1]["event"] == "complete"
            await runtime.aclose()
    asyncio.run(execute())
    result = UsageService(app.database).summary(agent_id)
    assert result["calls"] == 1
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 4
    assert result["provider_total_tokens"] == 20
    assert result["cost_usd"] is None
    assert result["cost_status"] == "unknown"
