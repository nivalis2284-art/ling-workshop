"""Browser boundary tests: owner authority, durable intents, and API contracts."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from lingnexus.api.browser import router
from lingnexus.browser import BrowserService
from lingnexus.browser.policy import ActionInput, OriginGrant, normalize_origin
from lingnexus.common.errors import Conflict, PermissionDenied, RevisionConflict


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://EXAMPLE.com:443", "https://example.com"),
        ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
        ("http://[::1]:8123", "http://[::1]:8123"),
    ],
)
def test_origins_are_exact_canonical_authorities(value, expected):
    assert normalize_origin(value, origin_only=True) == expected


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/test",
        "https://*.example.com",
        "https://u:p@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
        "https://example.com#fragment",
        "https://example.com\\@evil.test",
    ],
)
def test_invalid_origin_grants_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_origin(value, origin_only=True)


def test_local_grants_require_explicit_allow_local(app, agent_id, tmp_path):
    async def run():
        async with BrowserService(app.database, tmp_path / "app-data") as browser:
            with pytest.raises(PermissionDenied):
                await browser.configure_origins(
                    agent_id,
                    [OriginGrant(origin="http://127.0.0.1:8123")],
                    expected_revision=0,
                )
            granted = await browser.configure_origins(
                agent_id,
                [OriginGrant(origin="http://127.0.0.1:8123", allow_local=True)],
                expected_revision=0,
            )
            assert granted["revision"] == 1
            assert granted["grants"] == [{"origin": "http://127.0.0.1:8123", "allow_local": True}]
            await browser.configure_origins(agent_id, [], expected_revision=1)
            with pytest.raises(RevisionConflict):
                await browser.configure_origins(agent_id, [], expected_revision=1)

    asyncio.run(run())


def test_action_schema_cannot_grant_approve_or_launch_headed():
    base = {
        "request_id": "one",
        "action": "goto",
        "origin_revision": 0,
        "url": "http://127.0.0.1:8123/form",
    }
    for extra in [{"allow_local": True}, {"headless": False}, {"approve": True}]:
        with pytest.raises(ValueError):
            ActionInput.model_validate(base | extra)
    for action in ["approve", "configure_origins", "open_login", "evaluate"]:
        with pytest.raises(ValueError):
            ActionInput.model_validate(base | {"action": action})
    with pytest.raises(ValueError):
        ActionInput.model_validate(base | {"action": "fill", "selector": "input"})


def test_no_origin_grant_no_browser_launch_or_durable_action(app, agent_id, tmp_path):
    async def run():
        async with BrowserService(app.database, tmp_path / "app-data") as browser:
            with pytest.raises(PermissionDenied):
                await browser.submit(
                    agent_id,
                    ActionInput(
                        request_id="ungranted",
                        action="goto",
                        origin_revision=0,
                        url="http://127.0.0.1:8123/form",
                    ),
                )
            assert (await browser.state(agent_id))["page"] is None
            assert browser.pending_approvals(agent_id) == []
            with app.database.transaction() as db:
                assert db.execute("SELECT count(*) FROM browser_actions").fetchone()[0] == 0

    asyncio.run(run())


def test_executing_request_recovers_unknown_and_never_replays(app, agent_id, tmp_path):
    async def run():
        root = tmp_path / "app-data"
        request = ActionInput(
            request_id="uncertain", action="goto", origin_revision=0, url="https://example.com/form"
        )
        async with BrowserService(app.database, root) as browser:
            intent = browser.store.prepare(agent_id, request)
            browser.store.mark_executing(intent["id"])
        async with BrowserService(app.database, root) as reopened:
            result = await reopened.submit(agent_id, request)
            assert result["status"] == "unknown"
            assert result["error_code"] == "interrupted_execution"
            assert (await reopened.state(agent_id))["page"] is None
            with pytest.raises(Conflict):
                await reopened.submit(
                    agent_id, request.model_copy(update={"url": "https://example.com/other"})
                )

    asyncio.run(run())


def test_owner_api_is_authenticated_and_never_echoes_invalid_secrets(app, agent_id, tmp_path):
    async def run():
        async with BrowserService(app.database, tmp_path / "app-data") as browser:
            server = FastAPI()
            server.state.owner_token = "owner-test-token"
            server.state.allowed_origins = {"http://localhost"}
            server.state.browser = browser
            server.include_router(router)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server), base_url="http://localhost"
            ) as client:
                path = f"/agents/{agent_id}/browser/origins"
                assert (await client.get(path)).status_code == 401
                headers = {"Authorization": "Bearer owner-test-token"}
                assert (await client.get(path, headers=headers)).json()["revision"] == 0
                denied = await client.get(path, headers=headers | {"Origin": "https://evil.test"})
                assert denied.status_code == 403
                grant = await client.put(
                    path,
                    headers=headers,
                    json={
                        "expected_revision": 0,
                        "grants": [{"origin": "http://127.0.0.1:8123", "allow_local": True}],
                    },
                )
                assert grant.status_code == 200
                assert grant.json()["revision"] == 1
                bad = await client.post(
                    f"/agents/{agent_id}/browser/actions",
                    headers=headers,
                    json={"action": "approve", "value": "hidden-secret"},
                )
                assert bad.status_code == 422
                assert "hidden-secret" not in bad.text
                login = await client.post(
                    f"/agents/{agent_id}/browser/login",
                    headers=headers,
                    json={"url": "http://127.0.0.1:8123/", "origin_revision": 1},
                )
                assert login.status_code == 422  # Explicit headed=true is mandatory.

    asyncio.run(run())


def test_normal_chrome_profile_is_rejected_without_creating_files(app, tmp_path, monkeypatch):
    local = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    normal = local / "Google" / "Chrome" / "User Data"
    with pytest.raises(ValueError):
        BrowserService(app.database, normal)
    with pytest.raises(ValueError):
        BrowserService(app.database, normal / "Default")
    assert not local.exists()
