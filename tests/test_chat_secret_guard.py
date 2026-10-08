import asyncio

import httpx
import pytest
from test_chat_runtime import KEY, chunk, configure, request, wire

from lingnexus.runtime import ChatRuntime


@pytest.mark.parametrize("channel", ["content", "reasoning_content"])
def test_provider_credential_echo_split_across_fragments_is_never_persisted(app, agent_id, channel):
    configure(app)
    data = wire(
        chunk({channel: "normal "}),
        chunk({channel: KEY[:12]}),
        chunk({channel: KEY[12:]}),
        chunk(finish="stop"),
        "[DONE]",
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, content=data
                )
            )
        ) as client:
            runtime = ChatRuntime(app, client=client)
            value = runtime.submit(request(agent_id))
            events = [e async for e in runtime.events(value["id"])]
            assert events[-1]["event"] == "error"
            assert events[-1]["data"]["code"] == "credential_in_response"
            assert runtime.get_run(value["id"])["assistant_source"] is None
            combined = "".join(e["data"].get("text", "") for e in events)
            assert KEY not in combined
            with app.database.transaction() as db:
                assert KEY not in "".join(db.iterdump())
            await runtime.aclose()

    asyncio.run(run())
