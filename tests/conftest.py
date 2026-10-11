"""Shared fixtures for the first part's acceptance layer.

The `Application` assembled here is only the standalone Agent system: chat,
attachments, models, tools/tasks, permissions, maintenance and backups. The
long-term part (history/memory/identity/learning/skills/development/activities
and the segment/recall helpers) is intentionally absent. Cases that exercised
it were retired — see the `*.retired.md` notes next to this file.
"""

import pytest

TOKEN = "test-only-local-token"


@pytest.fixture
def app_factory():
    from lingnexus.app import Application

    return Application


@pytest.fixture
def app(tmp_path, app_factory):
    instance = app_factory(tmp_path / "ling.db")
    yield instance
    instance.close()


@pytest.fixture
def agent_id(app):
    return app.create_agent("灵")


@pytest.fixture
def headers():
    """Owner bearer token accepted by the local API boundary."""
    return {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def client(tmp_path):
    """A real composition root served through FastAPI's test transport."""
    from fastapi.testclient import TestClient

    from lingnexus.api.app import create_app

    server = create_app(
        data_dir=tmp_path, token=TOKEN, allowed_origins={"http://localhost:8765"}
    )
    with TestClient(server, base_url="http://localhost:8765") as value:
        yield value
