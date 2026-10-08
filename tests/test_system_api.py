import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lingnexus.api.system import router
from lingnexus.desktop.startup import RUN_KEY, VALUE_NAME


@pytest.fixture
def server(app):
    server = FastAPI()
    server.state.services = app
    server.state.owner_token = "test-system-owner"
    server.state.allowed_origins = {"http://localhost:8765"}
    server.include_router(router, prefix="/api")
    return server


def client(server):
    return TestClient(server, headers={"Authorization": "Bearer test-system-owner"})


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/backups", None),
        ("PUT", "/backups", {"enabled": False}),
        ("GET", "/startup", None),
        ("PUT", "/startup", {"enabled": True}),
        ("GET", "/restore", None),
        ("POST", "/restore/prepare", {"source_format": "backup", "source_path": "C:/unused"}),
        ("POST", "/restore/commit", {"plan_id": "a" * 32, "confirmed": True}),
        ("POST", "/restore/quit", {"plan_id": "a" * 32, "confirmed": True}),
    ],
)
def test_every_system_route_requires_owner_and_approved_origin(server, method, path, body):
    anonymous = TestClient(server)
    assert anonymous.request(method, "/api/system" + path, json=body).status_code == 401
    assert (
        client(server)
        .request(
            method, "/api/system" + path, json=body, headers={"Origin": "https://outside.invalid"}
        )
        .status_code
        == 403
    )


def test_backup_settings_save_and_failures_do_not_claim_success(server, app, agent_id, tmp_path):
    api = client(server)
    assert api.get("/api/system/backups").json()["enabled"] is True
    assert api.get("/api/system/backups").json()["last_success_at"] is None
    path = str(tmp_path / "chosen" / "backups")
    result = api.put("/api/system/backups", json={"path": path, "retention": 2, "enabled": False})
    assert result.status_code == 200
    assert result.json()["path"] == path
    assert result.json()["enabled"] is False
    assert api.put("/api/system/backups", json={"retention": 0}).status_code == 422
    assert api.put("/api/system/backups", json={"enabled": "true"}).status_code == 422
    assert api.put("/api/system/backups", json={"path": str(tmp_path)}).status_code == 422


def test_restore_commit_requires_preview_and_confirmation_and_quit_is_separate(
    server, app, agent_id, tmp_path
):
    api = client(server)
    quits = []
    server.state.request_quit = lambda: quits.append(True)
    source = tmp_path / "backup.db"
    app.maintenance.backup_to(source)
    assert (
        api.post(
            "/api/system/restore/quit", json={"plan_id": "a" * 32, "confirmed": True}
        ).status_code
        == 409
    )
    plan = api.post(
        "/api/system/restore/prepare", json={"source_format": "backup", "source_path": str(source)}
    )
    assert plan.status_code == 200
    plan_id = plan.json()["plan_id"]
    assert (
        api.post(
            "/api/system/restore/commit", json={"plan_id": plan_id, "confirmed": False}
        ).status_code
        == 422
    )
    assert api.post(
        "/api/system/restore/commit", json={"plan_id": "b" * 32, "confirmed": True}
    ).status_code in {409, 422}
    result = api.post("/api/system/restore/commit", json={"plan_id": plan_id, "confirmed": True})
    assert result.status_code == 200
    assert result.json()["status"] == "queued" and result.json()["quit_available"] is True
    assert quits == []
    intent = json.loads((tmp_path / "pending-restore.json").read_text())
    assert set(intent) == {"format", "version", "plan_id"}
    assert (
        api.post("/api/system/restore/quit", json={"plan_id": plan_id, "confirmed": True}).json()[
            "quit_requested"
        ]
        is True
    )
    assert quits == [True]


def test_restore_without_desktop_callback_requests_manual_restart(server, app, agent_id, tmp_path):
    source = tmp_path / "backup.db"
    app.maintenance.backup_to(source)
    api = client(server)
    plan = api.post(
        "/api/system/restore/prepare", json={"source_format": "backup", "source_path": str(source)}
    ).json()
    body = {"plan_id": plan["plan_id"], "confirmed": True}
    assert api.post("/api/system/restore/commit", json=body).json()["quit_available"] is False
    assert api.post("/api/system/restore/quit", json=body).json()["quit_requested"] is False


def test_startup_uses_current_user_registry_and_source_command(server, monkeypatch):
    values = {}
    accesses = []
    owner = object()

    @contextmanager
    def key(hive, path, **kwargs):
        accesses.append((hive, path))
        yield "key"

    def query(key, name):
        if name not in values:
            raise FileNotFoundError
        return values[name], 1

    def delete(key, name):
        values.pop(name, None)

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=owner,
        REG_SZ=1,
        KEY_SET_VALUE=2,
        OpenKey=key,
        CreateKeyEx=key,
        QueryValueEx=query,
        SetValueEx=lambda key, name, reserved, kind, value: values.update({name: value}),
        DeleteValue=delete,
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    api = client(server)
    assert api.get("/api/system/startup").json()["enabled"] is False
    assert api.put("/api/system/startup", json={"enabled": True}).json()["enabled"] is True
    command = values[VALUE_NAME]
    assert " -m lingnexus.desktop --background --data-dir " in command
    assert str(server.state.services.database.path.parent) in command
    assert all(hive is owner and path == RUN_KEY for hive, path in accesses)
    assert api.put("/api/system/startup", json={"enabled": False}).json()["enabled"] is False
    assert VALUE_NAME not in values
