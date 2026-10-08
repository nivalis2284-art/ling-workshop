-- Native continuation is run state only, never a long-term memory source.
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 objective TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 workspace TEXT, profile_id TEXT NOT NULL REFERENCES model_profiles(id), docker_image TEXT,
 status TEXT NOT NULL, step_count INTEGER NOT NULL DEFAULT 0,
 step_limit INTEGER NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
 final TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(agent_id, request_id)
);
CREATE TABLE task_checkpoints (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
 state TEXT NOT NULL
);
CREATE TABLE task_steps (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 model_step INTEGER NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
 arguments TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
 effect_id TEXT, result TEXT, created_at TEXT NOT NULL,
 UNIQUE(task_id, model_step, call_id)
);
CREATE TABLE task_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX task_events_cursor ON task_events(task_id, id);
CREATE TABLE task_reviews (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 manifest TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL
);
