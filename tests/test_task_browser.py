"""Real Chrome task loops on an owned local form; model HTTP remains synthetic."""
import asyncio
import json

import httpx
import pytest
import test_browser_live as browser_fixtures
from test_browser_live import CHROME
from test_tasks import chat, configure, settle

from lingnexus.browser import BrowserService, OriginGrant
from lingnexus.tasks import TaskRuntime

pytestmark = pytest.mark.skipif(not CHROME.is_file(), reason="installed Chrome required")


@pytest.fixture
def owned_form():
    yield from browser_fixtures.form_server.__wrapped__()


@pytest.mark.parametrize("outcome", ["verify", "unchecked", "wrong_confirmation", "rejected", "uncertain", "pause_during_prepare"])
def test_browser_task_owner_fence_and_actual_dom_verification(app, agent_id, tmp_path, owned_form, outcome):
    async def run():
        configure(app)
        origin, submissions = owned_form
        async with BrowserService(app.database, tmp_path / "task-browser", executable_path=CHROME) as browser:
            app.browser = browser
            await browser.configure_origins(agent_id, [OriginGrant(origin=origin, allow_local=True)], expected_revision=0)
            prepared, release = asyncio.Event(), asyncio.Event()
            if outcome == "pause_during_prepare":
                original_submit = browser.submit
                async def submit_then_hold(agent, request):
                    result = await original_submit(agent, request)
                    if result["status"] == "prepared":
                        prepared.set()
                        await release.wait()
                    return result
                browser.submit = submit_then_hold
            requests = []
            async def handler(request):
                body = json.loads(request.content)
                requests.append(body)
                stage = len(requests) - 1
                # Use only actual previous tool outputs to construct subsequent args.
                value = json.loads(body["messages"][-1]["content"]) if stage else None
                if stage == 0:
                    reply = chat(calls=[("state", "browser_state", {})])
                elif stage == 1:
                    reply = chat(calls=[("goto", "browser_goto", {"url": origin + "/override", "origin_revision": value["result"]["origins"]["revision"]})])
                elif stage == 2:
                    reply = chat(calls=[("read-form", "browser_read", {"origin_revision": 1, "page_version": value["result"]["page_version"]})])
                elif stage == 3:
                    reply = chat(calls=[("fill", "browser_fill", {"origin_revision": 1, "page_version": value["result"]["page_version"], "selector": "input[name=message]", "value": "synthetic task"})])
                elif stage == 4:
                    reply = chat(calls=[("send", "browser_request_click", {"origin_revision": 1, "page_version": value["result"]["page_version"], "selector": "#send", "action": "submit"})])
                elif outcome == "unchecked" or outcome == "rejected":
                    reply = chat("Everything succeeded!")
                elif stage == 5:
                    assert value["result"]["business_outcome"] == "not_verified"
                    reply = chat(calls=[("state-after", "browser_state", {})])
                elif stage == 6:
                    reply = chat(calls=[("check", "browser_read", {"origin_revision": value["result"]["origins"]["revision"], "page_version": value["result"]["page"]["page_version"], "expected_text": "Submission received" if outcome in {"verify", "pause_during_prepare"} else "Nonexistent receipt"})])
                else:
                    reply = chat("Checked the actual confirmation page.")
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=reply)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                runtime = TaskRuntime(app, client, max_steps=5)
                tid = await runtime.submit({"agent_id": agent_id, "objective": "Submit the owned synthetic form with owner approval, then check confirmation", "request_id": "browser-task"})
                if outcome == "pause_during_prepare":
                    await asyncio.wait_for(prepared.wait(), 10)
                    pausing = asyncio.create_task(runtime.pause(tid))
                    await asyncio.sleep(0)
                    release.set()
                    assert (await pausing)["status"] == "paused"
                    await runtime.resume(tid)
                state = await settle(runtime, tid)
                assert state["status"] == "waiting_owner", state
                approval = browser.pending_approvals(agent_id)[0]
                assert state["approval_id"] == approval["id"]
                assert not submissions and len(requests) == 5
                assert runtime.store.load(tid)["pending"][0]["id"] == "send"
                await runtime.pause(tid)
                assert runtime.get(tid)["approval_id"] == approval["id"]
                await runtime.resume(tid)  # Reconciliation works even at model-step budget.
                assert (await settle(runtime, tid))["status"] == "waiting_owner"
                assert len(browser.pending_approvals(agent_id)) == 1 and not submissions
                assert len(requests) == 5
                await runtime.aclose()
                recovered = TaskRuntime(app, client)
                await recovered.start()
                if outcome == "uncertain":
                    browser.store.mark_executing(approval["action_id"])
                    browser.store.finish(approval["action_id"], "unknown", error_code="effect_needs_review")
                else:
                    await browser.decide(approval["id"], decision="reject" if outcome == "rejected" else "approve", expected_intent_hash=approval["intent_hash"], agent_id=agent_id)
                await recovered.resume(tid, additional_steps=5)
                result = await settle(recovered, tid)
                expected_status = "completed" if outcome in {"verify", "pause_during_prepare"} else "blocked" if outcome == "uncertain" else "needs_review"
                assert result["status"] == expected_status, result
                assert result["verified"] == (expected_status == "completed")
                assert len(submissions) == (0 if outcome in {"rejected", "uncertain"} else 1)
                if submissions:
                    assert submissions[0]["message"] == ["synthetic task"]
                with app.database.transaction() as db:
                    clicks = db.execute("SELECT count(*) FROM browser_actions WHERE agent_id=? AND action='submit'", (agent_id,)).fetchone()[0]
                    assert clicks == 1
                assert {t["function"]["name"] for t in requests[0]["tools"]}.isdisjoint({"approve", "configure_origins", "open_login", "evaluate"})
                if outcome in {"verify", "pause_during_prepare"}:
                    read = next(e["data"]["result"] for e in recovered.events(tid) if e["type"] == "tool_result" and e["data"]["call_id"] == "check")
                    assert read["verification_passed"] and read["result"]["text"] == "Submission received"
                if outcome == "uncertain":
                    await recovered.resume(tid)
                    assert (await settle(recovered, tid))["status"] == "blocked"
                    assert not submissions and len(requests) == 5
                await recovered.aclose()
    asyncio.run(run())


def test_model_browser_tools_cannot_grant_or_approve(app, agent_id, tmp_path):
    async def run():
        from test_tasks import client_for
        configure(app)
        async with BrowserService(app.database, tmp_path / "no-grants", executable_path=CHROME) as browser:
            app.browser = browser
            replies = [chat(calls=[("denied", "browser_goto", {"url": "http://127.0.0.1:8123/form", "origin_revision": 0})]), chat(calls=[("approve", "browser_request_click", {"origin_revision": 0, "page_version": "none", "selector": "#send", "approve": True})]), chat("Done")]
            async with client_for(replies, []) as client:
                runtime = TaskRuntime(app, client)
                tid = await runtime.submit({"agent_id": agent_id, "objective": "ungranted request", "request_id": "ungranted"})
                result = await settle(runtime, tid)
                assert result["status"] == "needs_review"
                assert browser.current_origins(agent_id)["grants"] == []
                assert (await browser.state(agent_id))["page"] is None
                assert not browser.pending_approvals(agent_id)
                await runtime.aclose()
    asyncio.run(run())
