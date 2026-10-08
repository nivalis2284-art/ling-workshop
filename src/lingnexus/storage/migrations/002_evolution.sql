CREATE TABLE proposals (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, base_revision INTEGER NOT NULL,
 kind TEXT NOT NULL, entry_key TEXT NOT NULL, content TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(agent_id,request_id)
);
CREATE TABLE proposal_sources (
 proposal_id TEXT NOT NULL REFERENCES proposals(id), source_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL, PRIMARY KEY(proposal_id,source_id,source_revision),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
CREATE TABLE decisions (
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
 decision TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE self_entry_origins (
 agent_id TEXT NOT NULL REFERENCES agents(id), entry_key TEXT NOT NULL,
 proposal_id TEXT NOT NULL REFERENCES proposals(id), PRIMARY KEY(agent_id,entry_key)
);
CREATE TABLE episodes (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 title TEXT NOT NULL, summary TEXT NOT NULL, scope TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(agent_id,request_id)
);
CREATE TABLE episode_sources (
 episode_id TEXT NOT NULL REFERENCES episodes(id), source_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL, PRIMARY KEY(episode_id,source_id,source_revision),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
CREATE VIRTUAL TABLE episode_search USING fts5(episode_id UNINDEXED, title, summary, tokenize='trigram');
