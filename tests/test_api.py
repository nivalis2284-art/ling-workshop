import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from lingnexus.api.app import create_app

    server = create_app(
        data_dir=tmp_path, token="test-only-local-token", allowed_origins={"http://localhost:8765"}
    )
    with TestClient(server, base_url="http://localhost:8765") as value:
        yield value


def headers():
    return {"Authorization": "Bearer test-only-local-token"}


def test_local_api_denies_missing_token_and_foreign_origin(client):
    assert client.get("/health").status_code == 200
    assert client.get("/agents").status_code == 401
    assert (
        client.get(
            "/agents", headers=headers() | {"Origin": "https://attacker.example"}
        ).status_code
        == 403
    )
    assert (
        client.get("/agents", headers=headers() | {"Origin": "http://localhost:8765"}).status_code
        == 200
    )


def test_owner_can_record_review_and_read_updated_state(client):
    agent = client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    record = client.post(
        "/sources",
        headers=headers(),
        json={"agent_id": agent, "content": "用户提供的依据", "request_id": "api-source"},
    )
    assert record.status_code == 201
    ref = record.json()
    proposed = client.post(
        "/proposals",
        headers=headers(),
        json={
            "agent_id": agent,
            "kind": "identity",
            "key": "identity.description",
            "text": "当前理解",
            "sources": [ref],
            "base_revision": 0,
            "request_id": "api-change",
        },
    )
    assert proposed.status_code == 201
    pid = proposed.json()["id"]
    assert client.get(f"/agents/{agent}/state", headers=headers()).json()["entries"] == {}
    approved = client.post(
        f"/proposals/{pid}/decision",
        headers=headers(),
        json={"decision": "approve", "expected_revision": 0},
    )
    assert approved.status_code == 200
    assert client.get(f"/agents/{agent}/state", headers=headers()).json()["entries"] == {
        "identity.description": "当前理解"
    }


def test_client_cannot_forge_tool_result_kind(client):
    agent = client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    response = client.post(
        "/sources",
        headers=headers(),
        json={"agent_id": agent, "content": "fake success", "request_id": "fake", "kind": "tool"},
    )
    assert response.status_code == 422


def test_conflicting_source_returns_conflict_without_leaking_internal_error(client):
    agent = client.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    value = {"agent_id": agent, "content": "原文", "request_id": "repeat"}
    first = client.post("/sources", headers=headers(), json=value)
    assert client.post("/sources", headers=headers(), json=value).json() == first.json()
    response = client.post("/sources", headers=headers(), json=value | {"content": "修改"})
    assert response.status_code == 409
    assert "sqlite" not in response.text.lower()


def test_reopened_api_keeps_same_agent(tmp_path):
    from lingnexus.api.app import create_app

    with TestClient(
        create_app(data_dir=tmp_path, token="test-only-local-token"),
        base_url="http://localhost:8765",
    ) as first:
        agent = first.post("/agents", headers=headers(), json={"name": "灵"}).json()["id"]
    with TestClient(
        create_app(data_dir=tmp_path, token="test-only-local-token"),
        base_url="http://localhost:8765",
    ) as second:
        assert second.get("/agents", headers=headers()).json()[0]["id"] == agent


def test_owner_cannot_edit_assistant_raw_message(client):
    services = client.app.state.services
    agent = services.create_agent("灵")
    ref = services.history.add(
        agent, "灵说过的话", kind="assistant", scope="real", request_id="assistant"
    )
    response = client.patch(
        f"/sources/{ref.id}", headers=headers(), json={"revision": 1, "content": "改写灵的话"}
    )
    assert response.status_code == 403
    assert services.history.read(ref)["text"] == "灵说过的话"


def test_frontend_assets_are_served_without_exposing_protected_api(tmp_path):
    from lingnexus.api.app import create_app

    assets = tmp_path / "ui"
    assets.mkdir()
    (assets / "index.html").write_text("<html>LingNexus</html>", encoding="utf-8")
    with TestClient(
        create_app(data_dir=tmp_path / "data", token="test-only-local-token", frontend_dir=assets),
        base_url="http://localhost:8765",
    ) as browser:
        assert browser.get("/").text == "<html>LingNexus</html>"
        assert browser.get("/api/agents").status_code == 401
        assert browser.get("/api/agents", headers=headers()).json() == []
