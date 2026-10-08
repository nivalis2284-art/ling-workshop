"""Connection/transaction infrastructure only; business rules live in modules."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = local()
        self._lock = RLock()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _migrate(self) -> None:
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)"
            )
            for file in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                found = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (file.name,)
                ).fetchone()
                if found:
                    continue
                # Only trusted, bundled migration filenames and SQL are interpolated.
                script = "BEGIN IMMEDIATE;\n" + file.read_text(encoding="utf-8")
                script += "\nINSERT INTO schema_migrations VALUES ('" + file.name + "'); COMMIT;"
                connection.executescript(script)
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nested service operations join the same thread's atomic unit of work."""
        current = getattr(self._local, "connection", None)
        if current is not None:
            # Savepoints make a caught inner failure unable to leave partial work.
            depth = getattr(self._local, "depth", 0) + 1
            self._local.depth = depth
            name = f"nested_{depth}"
            callback_count = len(self._local.after_commit)
            current.execute(f"SAVEPOINT {name}")
            try:
                yield current
                current.execute(f"RELEASE {name}")
            except BaseException:
                del self._local.after_commit[callback_count:]
                current.execute(f"ROLLBACK TO {name}")
                current.execute(f"RELEASE {name}")
                raise
            finally:
                self._local.depth = depth - 1
            return
        with self._lock:
            connection = self.connect()
            self._local.connection = connection
            self._local.after_commit = []
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
                # Publish non-database effects only after the outer transaction commits,
                # before another thread can observe the new configuration.
                callbacks = self._local.after_commit
                self._local.after_commit = []
                for callback in callbacks:
                    callback()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._local.connection = None
                self._local.after_commit = []
                connection.close()

    def after_commit(self, callback: Callable[[], None]) -> None:
        if getattr(self._local, "connection", None) is None:
            raise RuntimeError("Commit callbacks require an active transaction")
        self._local.after_commit.append(callback)
