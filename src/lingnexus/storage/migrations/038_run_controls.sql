-- Durable owner intent for an active run. A pause request is not a model
-- terminal state: the run keeps its checkpoint and stops at the next tool/model
-- boundary. The coordinator may requeue this row's work only after the run is
-- safely closed. This table stores intent, never native replay material.
CREATE TABLE chat_run_controls (
    run_id TEXT PRIMARY KEY REFERENCES chat_runs(id) ON DELETE CASCADE,
    intent TEXT NOT NULL CHECK(intent IN ('running','pause')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    updated_at TEXT NOT NULL
);
INSERT INTO chat_run_controls(run_id,intent,revision,updated_at)
SELECT id,'running',1,created_at FROM chat_runs;
