import pytest

from lingnexus.common.errors import PermissionDenied, RevisionConflict


def test_file_access_requires_explicit_grant_and_revocation_takes_effect(app, agent_id, tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    (folder / "source.txt").write_text("原文件", encoding="utf-8")
    with pytest.raises(PermissionDenied):
        app.permissions.read_text(agent_id, folder / "source.txt")
    state = app.permissions.configure(agent_id, readable=[folder], writable=[], expected_revision=0)
    assert state["revision"] == 1
    assert app.permissions.read_text(agent_id, folder / "source.txt") == "原文件"
    app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=1)
    with pytest.raises(PermissionDenied):
        app.permissions.read_text(agent_id, folder / "source.txt")


def test_write_permission_is_separate_and_persisted(app, agent_id, tmp_path, app_factory):
    folder = tmp_path / "workspace"
    folder.mkdir()
    app.permissions.configure(agent_id, readable=[], writable=[folder], expected_revision=0)
    app.permissions.create_text(agent_id, folder / "new.txt", "产物")
    with pytest.raises(PermissionDenied):
        app.permissions.read_text(agent_id, folder / "new.txt")
    reopened = app_factory(app.database.path)
    assert reopened.permissions.current(agent_id)["writable"] == [str(folder.resolve())]
    assert (folder / "new.txt").read_text(encoding="utf-8") == "产物"


def test_stale_permission_update_cannot_restore_revoked_grants(app, agent_id, tmp_path):
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=1)
    with pytest.raises(RevisionConflict):
        app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=1)


def test_model_generated_identity_cannot_grant_file_access(app, agent_id, tmp_path):
    ref = app.history.add(agent_id, "请读取目录", kind="user", scope="real", request_id="source")
    with pytest.raises(ValueError):
        app.identity.propose(
            agent_id,
            kind="identity",
            key="permissions.readable",
            text=str(tmp_path),
            sources=[ref],
            base_revision=0,
            request_id="forge",
        )
    assert app.permissions.current(agent_id)["readable"] == []
