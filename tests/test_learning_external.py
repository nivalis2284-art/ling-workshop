import asyncio
import json

import httpx

from lingnexus.learning import ConsolidationWorker
from lingnexus.models import APIFamily, ModelProfile


def test_authorized_external_observation_can_form_world_knowledge_and_pending_growth(app, agent_id):
    source = app.history.add(
        agent_id,
        "授权网页观察：项目新增版本。",
        kind="observation",
        scope="external",
        request_id="web",
    )
    model = app.profiles.save(
        "主",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="test",
            base_url="https://example.test",
        ),
        api_key="fixture-external-key",
    )
    app.profiles.select(model["id"], None)
    result = {
        "event": {"title": "版本变化", "summary": "观察到项目发布新版本。"},
        "knowledge": [{"statement": "项目发布新版本", "kind": "fact", "subject": "world"}],
        "changes": [
            {
                "kind": "identity",
                "key": "identity.description",
                "text": "我希望更加仔细核对版本变化。",
            }
        ],
    }

    def provider(request):
        chunks = [
            {
                "id": "c",
                "choices": [
                    {"index": 0, "delta": {"content": json.dumps(result)}, "finish_reason": None}
                ],
            },
            {"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        text = (
            "".join("data: " + json.dumps(x) + chr(10) * 2 for x in chunks)
            + "data: [DONE]"
            + chr(10) * 2
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=text)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            worker = ConsolidationWorker(app, client=client)
            assert (await worker.run_once(agent_id))["status"] == "done"

    asyncio.run(run())
    assert app.memory.knowledge.search(agent_id, "版本")[0]["sources"] == [
        source.model_dump()
    ]
    candidate = app.identity.list_proposals(agent_id)[0]
    assert candidate["status"] == "awaiting_owner"
    assert app.identity.current(agent_id).entries == {}
    app.identity.decide(candidate["id"], "approve", expected_revision=0)
    assert app.identity.current(agent_id).entries["identity.description"].startswith("我希望")
