CREATE TABLE source_visibility (
 source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, active INTEGER NOT NULL,
 PRIMARY KEY(source_id,source_revision),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), title TEXT NOT NULL,
 scope TEXT NOT NULL, head_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE conversation_messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 parent_id TEXT REFERENCES conversation_messages(id), source_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL, role TEXT NOT NULL, request_id TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(conversation_id,request_id),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
