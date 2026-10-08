CREATE TABLE actions (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), request_id TEXT NOT NULL,
 name TEXT NOT NULL, arguments TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL, result TEXT, error_code TEXT, source_id TEXT, source_revision INTEGER,
 created_at TEXT NOT NULL, UNIQUE(agent_id,request_id),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
