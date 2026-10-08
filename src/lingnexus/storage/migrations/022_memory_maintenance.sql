ALTER TABLE knowledge ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1);

-- No FK to a derived entry: its audit must survive deletion of that entry.
CREATE TABLE memory_edits (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 target_kind TEXT NOT NULL CHECK(target_kind IN ('episodes','knowledge')),
 target_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 2),
 action TEXT NOT NULL CHECK(action IN ('correct','archive','restore','delete')),
 actor TEXT NOT NULL CHECK(actor = 'owner'),
 prior TEXT NOT NULL CHECK(json_valid(prior)),
 current TEXT NOT NULL CHECK(json_valid(current)), created_at TEXT NOT NULL,
 UNIQUE(target_kind,target_id,revision)
);
CREATE INDEX memory_edits_agent_time ON memory_edits(agent_id,created_at);
CREATE INDEX episodes_maintenance ON episodes(agent_id,scope,status,created_at);
CREATE INDEX knowledge_maintenance ON knowledge(agent_id,scope,status,created_at);
