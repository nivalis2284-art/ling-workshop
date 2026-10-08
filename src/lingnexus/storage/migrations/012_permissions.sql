CREATE TABLE permission_revisions (
 agent_id TEXT NOT NULL REFERENCES agents(id), revision INTEGER NOT NULL,
 readable TEXT NOT NULL, writable TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(agent_id,revision)
);
