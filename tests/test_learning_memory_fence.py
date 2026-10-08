import asyncio
import json

import httpx
from test_learning import add, configure, response, result_document

from lingnexus.learning import ConsolidationWorker
from lingnexus.memory.maintenance import MemoryMaintenance


def test_learning_rejects_owner_corrected_derived_memory_from_generation_snapshot(app, agent_id):
    configure(app)
    old = add(app, agent_id, "teaparty is on Friday", request_id="old")
    kid = app.memory.knowledge.save(
        agent_id, "teaparty is on Friday", [old], kind="fact", request_id="known"
    )
    with app.database.transaction() as db:
        db.execute("UPDATE jobs SET status='skipped'")
    add(app, agent_id, "teaparty recap", request_id="new")

    def provider(request):
        body = json.loads(request.content)
        assert "Friday" in str(body)
        MemoryMaintenance(app).correct(
            "knowledge", kid, expected_revision=1, statement="teaparty is on Saturday"
        )
        return response(
            result_document(
                event=None,
                knowledge=[
                    {"statement": "teaparty is on Friday", "kind": "fact", "subject": "world"}
                ],
            )
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            result = await ConsolidationWorker(app, client=client).run_once(agent_id)
            assert result["status"] == "stale"
            assert result["outcome"] == "source_invalid"

    asyncio.run(run())
    assert len(app.memory.knowledge.search(agent_id, "teaparty")) == 1
    assert "Saturday" in app.memory.knowledge.read(kid)["statement"]
