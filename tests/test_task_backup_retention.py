from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_task_assets_backup import create_paused_task

from lingnexus.maintenance.automatic import AutomaticBackups


def test_owned_task_backups_follow_retention_without_erasing_current_task_assets(
    app, agent_id, tmp_path
):
    tid, snapshot, _state = create_paused_task(app, agent_id, tmp_path)
    automatic = AutomaticBackups(app)
    automatic.configure(retention=1)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    first = Path(automatic.tick(now)["last_backup"])
    second = Path(automatic.tick(now + timedelta(days=1))["last_backup"])
    assert second.is_file()
    assert not first.exists()
    assert not Path(str(first) + ".assets").exists()
    assert snapshot.artifact(tid, "answer.txt") == b"changed answer\n"
