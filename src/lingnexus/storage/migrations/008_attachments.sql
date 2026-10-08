CREATE TABLE attachments (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), original_name TEXT NOT NULL,
 blob_name TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL,
 extraction TEXT NOT NULL, created_at TEXT NOT NULL
);
