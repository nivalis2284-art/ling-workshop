"""SQLite fault injection in temporary test databases only.

Each abort occurs *inside* the product transaction. This proves rollback and
restart behavior without replacing product persistence with a fake dictionary.
"""

from contextlib import contextmanager

POINTS = {
    "archive_before_outbox": ("BEFORE INSERT", "continuity_branch_returns", "1"),
    "publish_after_message": ("BEFORE UPDATE", "continuity_branch_returns", "NEW.status='published'"),
    "publish_after_outbox": ("BEFORE UPDATE", "continuity_work_branches", "NEW.return_state='published'"),
    "epoch_after_seal": ("BEFORE UPDATE", "chat_run_checkpoints",
                         "json_extract(NEW.state,'$.context_epoch') > coalesce(json_extract(OLD.state,'$.context_epoch'),0)"),
}


@contextmanager
def crash_at(database, point):
    event, table, condition = POINTS[point]
    with database.transaction() as db:
        db.execute(f"CREATE TRIGGER m2_test_crash {event} ON {table} WHEN {condition} "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic crash point'); END")
    try:
        yield
    finally:
        with database.transaction() as db:
            db.execute("DROP TRIGGER IF EXISTS m2_test_crash")
