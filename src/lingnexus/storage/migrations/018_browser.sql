CREATE TABLE browser_origin_revisions (
 agent_id TEXT NOT NULL REFERENCES agents(id), revision INTEGER NOT NULL,
 grants TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(agent_id,revision)
);
CREATE TABLE browser_actions (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 request_id TEXT NOT NULL, action TEXT NOT NULL, arguments TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','executing','completed','failed','unknown')),
 result TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(agent_id,request_id)
);
CREATE TABLE browser_approvals (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES browser_actions(id),
 agent_id TEXT NOT NULL REFERENCES agents(id), intent TEXT NOT NULL, intent_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','invalidated')),
 created_at TEXT NOT NULL, decided_at TEXT
);
CREATE INDEX browser_approvals_pending ON browser_approvals(agent_id,status,created_at);
