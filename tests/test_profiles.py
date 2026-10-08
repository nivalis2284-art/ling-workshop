import pytest

from lingnexus.common.errors import PermissionDenied
from lingnexus.models import APIFamily, ModelProfile


def profile():
    return ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="test-model",
        base_url="https://example.test",
    )


def test_profile_persists_but_secret_exists_only_in_memory(app, app_factory):
    item = app.profiles.save("主模型", profile(), api_key="fictional-secret-not-a-real-key")
    assert app.profiles.credentials(item["id"]).api_key == "fictional-secret-not-a-real-key"
    assert "fictional-secret" not in str(app.profiles.list())
    reopened = app_factory(app.database.path)
    assert reopened.profiles.list()[0]["profile"]["model"] == "test-model"
    assert reopened.profiles.list()[0]["credentials_available"] is False
    with pytest.raises(PermissionDenied):
        reopened.profiles.credentials(item["id"])
    with app.database.transaction() as db:
        assert "fictional-secret" not in "\n".join(db.iterdump())


def test_model_selection_rejects_missing_profile(app):
    from lingnexus.common.errors import NotFound

    with pytest.raises(NotFound):
        app.profiles.select("does-not-exist", None)


def test_changing_endpoint_does_not_reuse_an_old_endpoint_secret(app):
    first = app.profiles.save("原连接", profile(), api_key="test-endpoint-secret")
    other = profile().model_copy(update={"base_url": "https://different.example.test"})
    updated = app.profiles.save("另一连接", other, profile_id=first["id"])
    assert updated["credentials_available"] is False
    with pytest.raises(PermissionDenied):
        app.profiles.credentials(first["id"])


def test_rolled_back_profile_save_preserves_committed_runtime_key(app):
    item = app.profiles.save("原连接", profile(), api_key="fictional-original-key")
    with pytest.raises(RuntimeError), app.database.transaction():
        app.profiles.save(
            "未提交名称", profile(), api_key="fictional-replacement-key", profile_id=item["id"]
        )
        raise RuntimeError("rollback")
    assert app.profiles.get(item["id"])["name"] == "原连接"
    assert app.profiles.credentials(item["id"]).api_key == "fictional-original-key"


def test_inner_rollback_does_not_publish_key_after_outer_commit(app):
    item = app.profiles.save("原连接", profile(), api_key="fictional-original-key")
    with app.database.transaction(), pytest.raises(RuntimeError), app.database.transaction():
        app.profiles.save(
            "失效变化", profile(), api_key="fictional-bad-key", profile_id=item["id"]
        )
        raise RuntimeError("inner rollback")
    assert app.profiles.credentials(item["id"]).api_key == "fictional-original-key"


def test_primary_and_fallback_selection_persist_without_keys(app, app_factory):
    main = app.profiles.save("主", profile(), api_key="test-only-secret")
    fallback = app.profiles.save("备", profile(), api_key="test-only-fallback")
    app.profiles.select(main["id"], fallback["id"])
    reopened = app_factory(app.database.path)
    assert reopened.profiles.selection() == {"primary": main["id"], "fallback": fallback["id"]}


def test_invalid_profile_payload_does_not_echo_key(tmp_path):
    from fastapi.testclient import TestClient

    from lingnexus.api.app import create_app

    with TestClient(
        create_app(data_dir=tmp_path, token="owner-test-token"), base_url="http://localhost:8765"
    ) as client:
        response = client.post(
            "/api/models",
            headers={"Authorization": "Bearer owner-test-token"},
            json={
                "name": "x",
                "api_key": "fictional-secret-value",
                "profile": {"api_family": "invalid-family", "model": "test", "base_url": "invalid"},
            },
        )
        assert response.status_code == 422
        assert "fictional-secret-value" not in response.text
