-- Private runtime state: portable exports must exclude all three tables.
DROP INDEX IF EXISTS chat_one_running_per_agent;
CREATE UNIQUE INDEX chat_one_running_per_conversation ON chat_runs(conversation_id)
 WHERE status='running' AND conversation_id IS NOT NULL;

CREATE TABLE chat_context_epochs (
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 epoch INTEGER NOT NULL CHECK(epoch >= 0),
 state TEXT NOT NULL CHECK(length(CAST(state AS BLOB)) <= 8388608),
 sha256 TEXT NOT NULL,
 sealed_at TEXT NOT NULL,
 PRIMARY KEY(run_id,epoch)
);
-- A bounded archive per logical run; application enforces the 32 MiB total too.
CREATE TABLE execution_resource_leases (
 resource TEXT PRIMARY KEY,
 owner TEXT NOT NULL,
 token TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('held','unknown')),
 created_at TEXT NOT NULL,
 session_id TEXT,
 session_kind TEXT,
 agent_id TEXT,
 run_id TEXT
);
CREATE INDEX execution_leases_owner ON execution_resource_leases(owner);
CREATE TABLE execution_lease_reviews (
 token TEXT PRIMARY KEY,
 owner TEXT NOT NULL,
 observation TEXT NOT NULL,
 reviewed_at TEXT NOT NULL
);
CREATE TABLE chat_compaction_policies (
 agent_id TEXT PRIMARY KEY REFERENCES agents(id),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 policy TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE chat_context_reads (
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 ordinal INTEGER NOT NULL,
 source_run_id TEXT NOT NULL,
 record_ids TEXT NOT NULL,
 page_bytes INTEGER NOT NULL,
 envelope_bytes INTEGER NOT NULL,
 step INTEGER NOT NULL,
 PRIMARY KEY(run_id,ordinal)
);
-- Legacy Task application crosses Actions rather than the chat dispatcher.
-- Its ordinary persisted action fence participates in the same filesystem lane.
CREATE TRIGGER context_action_lock BEFORE UPDATE OF status ON actions
WHEN NEW.status='executing' AND OLD.status='prepared'
 AND NEW.name IN ('create_file','write_file')
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM execution_resource_leases WHERE resource='filesystem')
 THEN RAISE(ABORT,'Workspace effect is fenced') END;
 INSERT INTO execution_resource_leases(resource,owner,token,status,created_at) VALUES('filesystem','action:'||NEW.id,NEW.id,'held',NEW.created_at);
END;
CREATE TRIGGER context_action_unlock AFTER UPDATE OF status ON actions
WHEN NEW.status IN ('completed','failed') AND NEW.name IN ('create_file','write_file')
BEGIN
 DELETE FROM execution_resource_leases WHERE owner='action:'||NEW.id AND token=NEW.id AND status='held';
END;
CREATE TRIGGER context_action_unknown AFTER UPDATE OF status ON actions
WHEN NEW.status='unknown' AND NEW.name IN ('create_file','write_file')
BEGIN
 UPDATE execution_resource_leases SET status='unknown' WHERE owner='action:'||NEW.id AND token=NEW.id;
END;
