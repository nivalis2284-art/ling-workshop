CREATE TABLE agents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE sources (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 kind TEXT NOT NULL, scope TEXT NOT NULL, request_id TEXT NOT NULL,
 request_fingerprint TEXT NOT NULL, current_revision INTEGER NOT NULL,
 recorded_at TEXT NOT NULL, UNIQUE(agent_id,request_id)
);
CREATE TABLE source_revisions (
 source_id TEXT NOT NULL REFERENCES sources(id), revision INTEGER NOT NULL,
 content TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(source_id,revision)
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 kind TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL
);
CREATE TABLE self_revisions (
 agent_id TEXT NOT NULL REFERENCES agents(id), revision INTEGER NOT NULL,
 entries TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(agent_id,revision)
);
