"""Real Chrome, temporary profiles and an owned loopback form; no real websites."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from lingnexus.api.browser import router
from lingnexus.browser import BrowserService
from lingnexus.browser.policy import ActionInput, OriginGrant
from lingnexus.common.errors import Conflict, PermissionDenied

CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@pytest.fixture
def form_server():
    class Submissions(list):
        def __init__(self):
            super().__init__()
            self.get_hosts = []

    submissions = Submissions()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            submissions.get_hosts.append(self.headers.get("Host", ""))
            path = urlsplit(self.path).path
            if path == "/chain":
                self.send_response(302)
                self.send_header("Location", "/redirect")
                self.end_headers()
                return
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/form")
                self.end_headers()
                return
            if path == "/cookie":
                self.send_response(200)
                self.send_header("Set-Cookie", "synthetic_session=owned; Max-Age=3600; Path=/")
                self.end_headers()
                self.wfile.write(b"Owned synthetic login")
                return
            if path == "/session":
                body = self.headers.get("Cookie", "no session")
            elif path == "/done":
                body = "Submission received"
            elif path == "/override":
                body = """<form id=owned method=post action=/unused>
                <input name=message value=prepared>
                <button id=send type=submit name=mode value=send formaction=/submit>Send</button>
                </form><input form=owned name=external value=included>"""
            else:
                auto = 'oninput="this.form.requestSubmit()"' if path == "/auto" else ""
                if path == "/autosave":
                    auto = "oninput=\"fetch('/submit',{method:'POST',body:'message=unapproved'})\""
                race = (
                    "onmousedown=\"document.querySelector('#message').value='Changed during click'\""
                    if path == "/race"
                    else ""
                )
                body = f"""<!doctype html><html><body><h1>Owned contact form</h1>
                <p>{"Visible text " * 2000}</p>
                <form method=post action=/submit>
                <label>Name<input id=name name=name {auto}></label>
                <label>Message<textarea id=message name=message></textarea></label>
                <button id=send type=submit {race}>Send message</button></form>
                <div hidden>hidden text</div><script>const secret='script text';</script>
                </body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            submissions.append(parse_qs(content.decode()))
            self.send_response(303)
            self.send_header("Location", "/done")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", submissions
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def grant(browser, agent_id, origin):
    return await browser.configure_origins(
        agent_id, [OriginGrant(origin=origin, allow_local=True)], expected_revision=0
    )


async def action(browser, agent_id, request_id, name, **kwargs):
    state = await browser.state(agent_id)
    return await browser.submit(
        agent_id,
        ActionInput(
            request_id=request_id,
            action=name,
            origin_revision=state["origins"]["revision"],
            page_version=state["page"]["page_version"] if state["page"] else None,
            **kwargs,
        ),
    )


@pytest.fixture(autouse=True)
def require_chrome():
    if not CHROME.is_file():
        pytest.skip("Installed Chrome executable is required for live browser tests")


def test_approved_form_submits_once_with_durable_exact_intent(app, agent_id, tmp_path, form_server):
    origin, submissions = form_server

    async def run():
        async with BrowserService(
            app.database, tmp_path / "app-data", executable_path=CHROME
        ) as browser:
            with pytest.raises(PermissionDenied):
                await action(browser, agent_id, "denied", "goto", url=origin + "/form")
            await grant(browser, agent_id, origin)
            assert (await action(browser, agent_id, "open", "goto", url=origin + "/form"))[
                "status"
            ] == "completed"
            read = await action(browser, agent_id, "read", "read", max_chars=97)
            assert len(read["result"]["text"]) == 97
            assert read["result"]["truncated"] is True
            assert "script text" not in read["result"]["text"]
            await action(
                browser, agent_id, "name", "fill", selector="#name", value="Synthetic Owner"
            )
            await action(
                browser, agent_id, "message", "fill", selector="#message", value="Owned test only"
            )
            state = await browser.state(agent_id)
            request = ActionInput(
                request_id="send",
                action="click",
                origin_revision=1,
                page_version=state["page"]["page_version"],
                selector="#send",
            )
            first, duplicate = await asyncio.gather(
                browser.submit(agent_id, request), browser.submit(agent_id, request)
            )
            assert first["id"] == duplicate["id"]
            assert first["status"] == "prepared"
            assert submissions == []
            pending = browser.pending_approvals(agent_id)
            assert len(pending) == 1
            approval = pending[0]
            assert approval["intent"]["form"]["fields"][0]["value"] == "Synthetic Owner"
            with pytest.raises(Conflict):
                await browser.decide(
                    approval["id"], decision="approve", expected_intent_hash="wrong"
                )
            assert submissions == []
            approved = await browser.decide(
                approval["id"], decision="approve", expected_intent_hash=approval["intent_hash"]
            )
            assert approved["status"] == "completed"
            assert submissions == [{"name": ["Synthetic Owner"], "message": ["Owned test only"]}]
            assert (await browser.submit(agent_id, request))["id"] == first["id"]
            assert (
                await browser.decide(
                    approval["id"], decision="approve", expected_intent_hash=approval["intent_hash"]
                )
            )["status"] == "completed"
            assert len(submissions) == 1
            assert browser.pending_approvals(agent_id) == []

    asyncio.run(run())


def test_click_already_sent_before_restart_recovers_unknown_without_resubmission(
    app, agent_id, tmp_path, form_server
):
    origin, submissions = form_server

    async def run():
        root = tmp_path / "app-data"
        async with BrowserService(app.database, root, executable_path=CHROME) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "open", "goto", url=origin + "/form")
            await action(browser, agent_id, "fill", "fill", selector="#name", value="Uncertain")
            page = (await browser.state(agent_id))["page"]
            request = ActionInput(
                request_id="send",
                action="click",
                origin_revision=1,
                page_version=page["page_version"],
                selector="#send",
            )
            prepared = await browser.submit(agent_id, request)
            approval = browser.pending_approvals(agent_id)[0]
            # Reproduce the crash window after the external effect, before durable completion.
            assert browser.store.approve(approval["id"]) is True
            await browser.driver.click(agent_id, approval["intent"])
            assert submissions == [{"name": ["Uncertain"]}]
            assert browser.get_action(agent_id, prepared["id"])["status"] == "executing"
        async with BrowserService(app.database, root, executable_path=CHROME) as reopened:
            duplicate = await reopened.submit(agent_id, request)
            assert duplicate["status"] == "unknown"
            assert duplicate["error_code"] == "interrupted_execution"
            assert (
                await reopened.decide(
                    approval["id"], decision="approve", expected_intent_hash=approval["intent_hash"]
                )
            )["status"] == "unknown"
            assert (await reopened.state(agent_id))["page"] is None
            assert len(submissions) == 1

    asyncio.run(run())


def test_owner_api_displays_effective_form_action_and_rejects_intent(
    app, agent_id, tmp_path, form_server
):
    origin, submissions = form_server

    async def run():
        async with BrowserService(
            app.database, tmp_path / "app-data", executable_path=CHROME
        ) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "open", "goto", url=origin + "/override")
            prepared = await action(browser, agent_id, "publish", "publish", selector="#send")
            server = FastAPI()
            server.state.owner_token = "owner-test-token"
            server.state.allowed_origins = {"http://localhost"}
            server.state.browser = browser
            server.include_router(router)
            prefix = f"/agents/{agent_id}/browser"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server), base_url="http://localhost"
            ) as client:
                headers = {"Authorization": "Bearer owner-test-token"}
                response = await client.get(prefix + "/approvals", headers=headers)
                approval = response.json()[0]
                assert approval["intent"]["form"]["action"] == origin + "/submit"
                assert any(
                    f["name"] == "external" and f["value"] == "included"
                    for f in approval["intent"]["form"]["fields"]
                )
                decision_path = prefix + f"/approvals/{approval['id']}/decision"
                payload = {"decision": "approve", "expected_intent_hash": approval["intent_hash"]}
                assert (await client.post(decision_path, json=payload)).status_code == 401
                payload["decision"] = "reject"
                rejected = await client.post(decision_path, headers=headers, json=payload)
                assert rejected.json()["error_code"] == "owner_rejected"
                payload["decision"] = "approve"
                assert (await client.post(decision_path, headers=headers, json=payload)).json()[
                    "status"
                ] == "failed"
                result = await client.get(prefix + f"/actions/{prepared['id']}", headers=headers)
                assert result.json()["status"] == "failed"
                assert (await client.get(prefix + "/state", headers=headers)).json()["page"]
                assert (await client.get(prefix + "/approvals", headers=headers)).json() == []
                assert submissions == []

    asyncio.run(run())


def test_script_changes_to_field_values_invalidate_approval_without_dom_mutation(
    app, agent_id, tmp_path, form_server
):
    origin, submissions = form_server

    async def run():
        async with BrowserService(
            app.database, tmp_path / "app-data", executable_path=CHROME
        ) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "open", "goto", url=origin + "/form")
            await action(browser, agent_id, "send", "send", selector="#send")
            approval = browser.pending_approvals(agent_id)[0]
            await browser.driver.sessions[agent_id].page.evaluate(
                "() => { document.querySelector('#name').value = 'Script changed intent'; }"
            )
            rejected = await browser.decide(
                approval["id"], decision="approve", expected_intent_hash=approval["intent_hash"]
            )
            assert rejected["error_code"] == "stale_page"
            assert submissions == []

    asyncio.run(run())


def test_fill_never_auto_submits_and_revocation_denies_existing_page(
    app, agent_id, tmp_path, form_server
):
    origin, submissions = form_server

    async def run():
        async with BrowserService(
            app.database, tmp_path / "app-data", executable_path=CHROME
        ) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "open", "goto", url=origin + "/auto")
            await action(browser, agent_id, "fill", "fill", selector="#name", value="Prepared")
            assert submissions == []
            await action(browser, agent_id, "send", "click", selector="#send")
            pending = browser.pending_approvals(agent_id)[0]
            previous_page = (await browser.state(agent_id))["page"]["page_version"]
            await browser.configure_origins(agent_id, [], expected_revision=1)
            for method, kwargs in [
                ("read", {}),
                ("fill", {"selector": "#name", "value": "bad"}),
                ("click", {"selector": "#send"}),
            ]:
                with pytest.raises(PermissionDenied):
                    await browser.submit(
                        agent_id,
                        ActionInput(
                            request_id="revoked-" + method,
                            action=method,
                            origin_revision=1,
                            page_version=previous_page,
                            **kwargs,
                        ),
                    )
            denied = await browser.decide(
                pending["id"], decision="approve", expected_intent_hash=pending["intent_hash"]
            )
            assert denied["status"] == "failed"
            assert submissions == []

    asyncio.run(run())


def test_changed_page_and_restarted_approval_cannot_execute(app, agent_id, tmp_path, form_server):
    origin, submissions = form_server

    async def run():
        root = tmp_path / "app-data"
        async with BrowserService(app.database, root, executable_path=CHROME) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "open", "goto", url=origin + "/form")
            await action(browser, agent_id, "send", "click", selector="#send")
            approval = browser.pending_approvals(agent_id)[0]
            await action(
                browser, agent_id, "change", "fill", selector="#message", value="Changed intent"
            )
            stale = await browser.decide(
                approval["id"], decision="approve", expected_intent_hash=approval["intent_hash"]
            )
            assert stale["status"] == "failed"
            assert stale["error_code"] == "stale_page"
            await action(browser, agent_id, "another", "click", selector="#send")
            second = browser.pending_approvals(agent_id)[0]
        async with BrowserService(app.database, root, executable_path=CHROME) as reopened:
            stale = await reopened.decide(
                second["id"], decision="approve", expected_intent_hash=second["intent_hash"]
            )
            assert stale["status"] == "failed"
            assert stale["error_code"] == "browser_session_ended"
            assert submissions == []

    asyncio.run(run())


def test_redirect_to_ungranted_origin_is_blocked_and_cookies_use_only_automation_profile(
    app, agent_id, tmp_path, form_server
):
    origin, submissions = form_server

    async def run():
        root = tmp_path / "app-data"
        async with BrowserService(app.database, root, executable_path=CHROME) as browser:
            await grant(browser, agent_id, origin)
            redirected = await action(
                browser, agent_id, "redirect", "goto", url=origin + "/redirect"
            )
            assert redirected["status"] == "failed"
            assert not any(host.startswith("localhost:") for host in submissions.get_hosts)
            chained = await action(browser, agent_id, "chain", "goto", url=origin + "/chain")
            assert chained["status"] == "failed"
            assert not any(host.startswith("localhost:") for host in submissions.get_hosts)
            await action(browser, agent_id, "cookie", "goto", url=origin + "/cookie")
        async with BrowserService(app.database, root, executable_path=CHROME) as reopened:
            await action(reopened, agent_id, "session", "goto", url=origin + "/session")
            read = await action(reopened, agent_id, "read-session", "read")
            assert "synthetic_session=owned" in read["result"]["text"]
            assert submissions == []
            other = app.create_agent("Second synthetic owner")
            await grant(reopened, other, origin)
            await action(reopened, other, "session", "goto", url=origin + "/session")
            other_read = await action(reopened, other, "read", "read")
            assert other_read["result"]["text"] == "no session"
        assert list((root / "browser" / "automation-profiles").glob("*/Default/Network/Cookies"))

    asyncio.run(run())


def test_fill_blocks_autosave_network_write_and_click_guard_checks_at_dispatch(
    app, agent_id, tmp_path, form_server
):
    origin, submissions = form_server

    async def run():
        async with BrowserService(
            app.database, tmp_path / "app-data", executable_path=CHROME
        ) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "autosave", "goto", url=origin + "/autosave")
            page = browser.driver.sessions[agent_id].page
            async with page.expect_event("requestfailed", predicate=lambda r: r.method == "POST"):
                filled = await action(
                    browser, agent_id, "fill", "fill", selector="#name", value="Prepared"
                )
            assert filled["result"]["submitted"] is False
            assert submissions == []
            await action(browser, agent_id, "race", "goto", url=origin + "/race")
            await action(browser, agent_id, "send", "click", selector="#send")
            approval = browser.pending_approvals(agent_id)[0]
            result = await browser.decide(
                approval["id"], decision="approve", expected_intent_hash=approval["intent_hash"]
            )
            assert result["status"] == "unknown"
            assert result["error_code"] == "effect_needs_review"
            assert submissions == []

    asyncio.run(run())


def test_model_cannot_close_or_operate_owner_manual_login_tab(app, agent_id, tmp_path, form_server):
    origin, submissions = form_server

    async def run():
        async with BrowserService(
            app.database, tmp_path / "app-data", executable_path=CHROME
        ) as browser:
            await grant(browser, agent_id, origin)
            await action(browser, agent_id, "open", "goto", url=origin + "/form")
            version = (await browser.state(agent_id))["page"]["page_version"]
            session = browser.driver.sessions[agent_id]
            # Exercise manual-mode arbitration using a real HEADLESS tab. Tests never
            # open a visible owner login window or use anyone's real credentials.
            session.manual = True
            for method, kwargs in [
                ("goto", {"url": origin + "/form"}),
                ("read", {}),
                ("fill", {"selector": "#name", "value": "Unapproved"}),
                ("click", {"selector": "#send"}),
            ]:
                with pytest.raises(PermissionDenied):
                    await browser.submit(
                        agent_id,
                        ActionInput(
                            request_id="manual-" + method,
                            action=method,
                            origin_revision=1,
                            page_version=version,
                            **kwargs,
                        ),
                    )
                assert not session.page.is_closed()
            assert (await browser.state(agent_id))["page"]["manual_login"] is True
            assert submissions == []
            await browser.finish_login(agent_id)
            assert session.page.is_closed()
            assert (await browser.state(agent_id))["page"] is None

    asyncio.run(run())
