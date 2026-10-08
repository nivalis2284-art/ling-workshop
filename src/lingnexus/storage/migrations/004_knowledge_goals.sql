CREATE TABLE knowledge (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), statement TEXT NOT NULL,
 kind TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(agent_id,request_id)
);
CREATE TABLE knowledge_sources (
 knowledge_id TEXT NOT NULL REFERENCES knowledge(id), source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 PRIMARY KEY(knowledge_id,source_id,source_revision),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
CREATE TABLE goals (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, current_revision INTEGER NOT NULL, UNIQUE(agent_id,request_id)
);
CREATE TABLE goal_revisions (
 goal_id TEXT NOT NULL REFERENCES goals(id), revision INTEGER NOT NULL, title TEXT NOT NULL,
 status TEXT NOT NULL, progress TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(goal_id,revision)
);
CREATE TABLE goal_sources (
 goal_id TEXT NOT NULL REFERENCES goals(id), source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 PRIMARY KEY(goal_id,source_id,source_revision),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
