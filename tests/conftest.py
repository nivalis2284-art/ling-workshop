import pytest


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
