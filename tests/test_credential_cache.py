"""Real Windows encrypted cache: restart, revocation, endpoint binding and backup."""

import json
import sys

import pytest
from fastapi.testclient import TestClient
from test_profiles import profile

from lingnexus.app import Application
from lingnexus.common.errors import PermissionDenied
from lingnexus.models import APIFamily

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
KEY = "synthetic-cache-key-never-a-real-service-key"


def opened(tmp_path):
    return Application(tmp_path / "ling.db", credential_cache=True)


def test_restart_reads_encrypted_cache_without_database_or_api_disclosure(tmp_path):
    app = opened(tmp_path)
    item = app.profiles.save("主模型", profile(), api_key=KEY)
    cache = tmp_path / "cache/model-credentials.bin"
    assert cache.is_file() and KEY.encode() not in cache.read_bytes()
    with app.database.transaction() as db:
        assert KEY not in "".join(db.iterdump())
    assert KEY not in json.dumps(app.profiles.list())
    app.close()
    restarted = opened(tmp_path)
    assert restarted.profiles.credentials(item["id"]).api_key == KEY
    assert restarted.profiles.get(item["id"])["credentials_available"]
    restarted.close()


def test_clear_cache_removes_disk_and_memory_but_keeps_profiles(tmp_path):
    app = opened(tmp_path)
    item = app.profiles.save("主模型", profile(), api_key=KEY)
    app.profiles.clear_cached_credentials()
    assert not (tmp_path / "cache/model-credentials.bin").exists()
    with pytest.raises(PermissionDenied):
        app.profiles.credentials(item["id"])
    restarted = opened(tmp_path)
    assert len(restarted.profiles.list()) == 1
    assert not restarted.profiles.get(item["id"])["credentials_available"]


def test_endpoint_change_discards_cache_and_cannot_resurrect_old_key(tmp_path):
    app = opened(tmp_path)
    item = app.profiles.save("模型", profile(), api_key=KEY)
    other = profile().model_copy(update={"base_url": "https://different.example.test"})
    app.profiles.save("模型", other, profile_id=item["id"])
    app.close()
    restarted = opened(tmp_path)
    restarted.profiles.save("模型", profile(), profile_id=item["id"])
    assert not restarted.profiles.get(item["id"])["credentials_available"]


def test_multiple_providers_keep_independent_keys_and_model_selection(tmp_path):
    app = opened(tmp_path)
    first = app.profiles.save("OpenAI 官方", profile(), api_key="first-provider-test-key")
    second_profile = profile().model_copy(
        update={
            "api_family": APIFamily.ANTHROPIC_MESSAGES,
            "base_url": "https://second.example.test",
        }
    )
    second = app.profiles.save("Anthropic 官方", second_profile, api_key="second-provider-test-key")
    app.profiles.select(first["id"], second["id"])
    app.close()
    restarted = opened(tmp_path)
    assert len(restarted.profiles.list()) == 2
    assert restarted.profiles.credentials(first["id"]).api_key == "first-provider-test-key"
    assert restarted.profiles.credentials(second["id"]).api_key == "second-provider-test-key"
    assert restarted.profiles.selection() == {"primary": first["id"], "fallback": second["id"]}
    restarted.profiles.save(
        "OpenAI 官方", profile(), api_key="replaced-test-key", profile_id=first["id"]
    )
    assert opened(tmp_path).profiles.credentials(second["id"]).api_key == "second-provider-test-key"


def test_rollback_does_not_replace_durable_key(tmp_path):
    app = opened(tmp_path)
    item = app.profiles.save("模型", profile(), api_key=KEY)
    original = (tmp_path / "cache/model-credentials.bin").read_bytes()
    with pytest.raises(RuntimeError), app.database.transaction():
        app.profiles.save("模型", profile(), api_key="replacement", profile_id=item["id"])
        raise RuntimeError("rollback")
    assert (tmp_path / "cache/model-credentials.bin").read_bytes() == original
    assert opened(tmp_path).profiles.credentials(item["id"]).api_key == KEY


def test_cache_missing_or_corrupt_allows_startup_without_credentials(tmp_path):
    app = opened(tmp_path)
    item = app.profiles.save("模型", profile(), api_key=KEY)
    (tmp_path / "cache/model-credentials.bin").write_bytes(b"not-dpapi")
    restarted = opened(tmp_path)
    assert not restarted.profiles.get(item["id"])["credentials_available"]
    assert restarted.profiles.cache_status()["error"]
    restarted.profiles.clear_cached_credentials()
    assert restarted.profiles.cache_status()["error"] is None


def test_backup_and_export_exclude_credential_cache(tmp_path):
    app = opened(tmp_path / "data")
    app.profiles.save("模型", profile(), api_key=KEY)
    export = tmp_path / "export"
    app.maintenance.export_to(export)
    app.maintenance.backup_to(tmp_path / "backup.db")
    assert not list(export.rglob("model-credentials*"))
    restored = tmp_path / "restored/ling.db"
    app.maintenance.restore_backup(tmp_path / "backup.db", restored)
    assert not Application(restored, credential_cache=True).profiles.list()[0][
        "credentials_available"
    ]


def test_owner_only_api_clear_and_restart_persistence(tmp_path):
    from lingnexus.api.app import create_app

    auth = {"Authorization": "Bearer owner-cache-test"}
    with TestClient(
        create_app(data_dir=tmp_path, token="owner-cache-test"), base_url="http://localhost:8765"
    ) as client:
        saved = client.post(
            "/api/models",
            headers=auth,
            json={"name": "模型", "profile": profile().model_dump(mode="json"), "api_key": KEY},
        )
        assert saved.status_code == 201
    with TestClient(
        create_app(data_dir=tmp_path, token="owner-cache-test"), base_url="http://localhost:8765"
    ) as client:
        assert client.get("/api/models", headers=auth).json()[0]["credentials_available"]
        assert client.delete("/api/models/credentials/cache").status_code == 401
        status = client.delete("/api/models/credentials/cache", headers=auth)
        assert status.status_code == 200 and status.json()["cached_profiles"] == 0
        assert not client.get("/api/models", headers=auth).json()[0]["credentials_available"]
        assert KEY not in status.text
