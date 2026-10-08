CREATE TABLE chat_runs (
 id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES agents(id),
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 status TEXT NOT NULL CHECK(status IN (
   'running','completed','failed','cancelled','interrupted'
 )),
 user_source_id TEXT NOT NULL, user_source_revision INTEGER NOT NULL,
 assistant_source_id TEXT, assistant_source_revision INTEGER,
 context_sources TEXT NOT NULL,
 created_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(agent_id,request_id),
 FOREIGN KEY(user_source_id,user_source_revision)
   REFERENCES source_revisions(source_id,revision),
 FOREIGN KEY(assistant_source_id,assistant_source_revision)
   REFERENCES source_revisions(source_id,revision)
);
CREATE UNIQUE INDEX chat_one_running_per_agent ON chat_runs(agent_id)
 WHERE status='running';
CREATE TABLE chat_events (
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 sequence INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(run_id,sequence)
);
