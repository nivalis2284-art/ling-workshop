from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from lingnexus.maintenance.automatic import AutomaticBackups


def test_daily_backup_is_verified_persistent_and_once_per_local_day(app, agent_id):
    automatic = AutomaticBackups(app)
    now = datetime(2026, 10, 9, 8, tzinfo=timezone(timedelta(hours=8)))
    result = automatic.tick(now)
    assert result["created"] is True
    source = Path(result["last_backup"])
    assert source.parent == app.database.path.parent / "backups"
    assert result["last_success_day"] == "2026-10-09"
    assert result["last_success_at"] == "2026-10-09T00:00:00+00:00"
    app.maintenance.restore_backup(source, source.parent / "check" / "ling.db")
    assert AutomaticBackups(app).tick(now)["created"] is False
    assert automatic.tick(now + timedelta(days=1))["created"] is True


def test_failed_backup_does_not_claim_day_and_can_retry(app, agent_id, monkeypatch):
    automatic = AutomaticBackups(app)
    backup = app.maintenance.backup_to
    monkeypatch.setattr(app.maintenance, "backup_to", lambda path: (_ for _ in ()).throw(OSError()))
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert automatic.tick(now)["last_success_day"] is None
    assert automatic.status()["last_error"]
    monkeypatch.setattr(app.maintenance, "backup_to", backup)
    assert automatic.tick(now)["created"]


def test_configure_and_retention_preserve_manual_corrupt_and_unowned_files(app, agent_id):
    automatic = AutomaticBackups(app)
    directory = app.database.path.parent / "custom" / "backups"
    automatic.configure(path=str(directory), retention=1)
    manual = directory / "manual.db"
    app.maintenance.backup_to(manual)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    first = Path(automatic.tick(now)["last_backup"])
    corrupt = Path(automatic.tick(now + timedelta(days=1))["last_backup"])
    assert not first.exists()
    assert not Path(str(first) + ".assets").exists()
    corrupt.write_bytes(b"leave corrupt evidence intact")
    latest = Path(automatic.tick(now + timedelta(days=2))["last_backup"])
    assert latest.exists() and corrupt.exists() and manual.exists()
    assert AutomaticBackups(app).status()["retention"] == 1
    automatic.configure(enabled=False)
    assert not automatic.tick(now + timedelta(days=3))["created"]


@pytest.mark.parametrize("path", ["/", "..", "relative/backups"])
def test_configure_rejects_root_or_relative_destinations(app, path):
    with pytest.raises(ValueError):
        AutomaticBackups(app).configure(path=path)


def test_retention_preserves_unknown_files_in_owned_assets(app, agent_id):
    automatic = AutomaticBackups(app)
    automatic.configure(retention=1)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    first = Path(automatic.tick(now)["last_backup"])
    sentinel = Path(str(first) + ".assets") / "owner-notes.txt"
    sentinel.write_text("not owned by retention")
    automatic.tick(now + timedelta(days=1))
    assert first.exists() and sentinel.read_text() == "not owned by retention"


def test_concurrent_ticks_from_two_instances_make_one_daily_backup(app, agent_id):
    from concurrent.futures import ThreadPoolExecutor

    first, second = AutomaticBackups(app), AutomaticBackups(app)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda service: service.tick(now), [first, second]))
    assert sum(result["created"] for result in results) == 1
    assert len(list((app.database.path.parent / "backups").glob("auto-*.db"))) == 1


def test_retention_preserves_linked_assets_and_does_not_touch_external_files(
    app, agent_id, tmp_path
):
    automatic = AutomaticBackups(app)
    automatic.configure(retention=1)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    first = Path(automatic.tick(now)["last_backup"])
    victim = tmp_path / "external.txt"
    victim.write_bytes(b"not backup data")
    try:
        (Path(str(first) + ".assets") / "linked.txt").symlink_to(victim)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")
    automatic.tick(now + timedelta(days=1))
    assert first.exists() and victim.read_bytes() == b"not backup data"
