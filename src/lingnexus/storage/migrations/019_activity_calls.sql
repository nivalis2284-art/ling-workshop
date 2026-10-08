CREATE TABLE activity_model_calls(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),activity_id TEXT NOT NULL,usage TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL);
