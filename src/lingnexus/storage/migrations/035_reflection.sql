-- M2 R4/R5: durable daily reflection policy, jobs, frozen snapshots and
-- call ledger. This module never owns the main transcript or published
-- experiences; those remain in A's continuity domain.
--
-- Calendar occurrences are keyed by the policy revision and a UTC instant.
-- A later policy revision starts a fresh calendar series, while revisions of
-- the same series can never create two live jobs for one slot. Budget rows are
-- fixed UTC-day buckets and are independent of the calendar timezone.

CREATE TABLE reflection_policies (
  agent_id TEXT PRIMARY KEY REFERENCES agents(id),
  revision INTEGER NOT NULL CHECK(revision >= 1),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
  local_time TEXT NOT NULL,
  timezone TEXT NOT NULL,
  min_age_days INTEGER NOT NULL CHECK(min_age_days >= 0),
  threshold_bytes INTEGER NOT NULL CHECK(threshold_bytes > 0),
  profile_id TEXT REFERENCES model_profiles(id),
  max_calls INTEGER NOT NULL CHECK(max_calls > 0),
  max_source_bytes INTEGER NOT NULL CHECK(max_source_bytes > 0),
  max_events INTEGER NOT NULL CHECK(max_events > 0),
  daily_call_limit INTEGER NOT NULL CHECK(daily_call_limit > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE reflection_slots (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  policy_revision INTEGER NOT NULL CHECK(policy_revision >= 1),
  timezone TEXT NOT NULL,
  local_date TEXT NOT NULL,
  requested_time TEXT NOT NULL,
  fold INTEGER NOT NULL CHECK(fold IN (0,1)),
  due_utc TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('due','leased','done','superseded')),
  job_id TEXT,
  lease_token TEXT,
  lease_until REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(agent_id,policy_revision,due_utc)
);
CREATE INDEX reflection_slots_due ON reflection_slots(status,due_utc);
CREATE UNIQUE INDEX reflection_slots_live
  ON reflection_slots(agent_id) WHERE status IN ('due','leased');

CREATE TABLE reflection_jobs (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(policy_revision >= 0),
  trigger TEXT NOT NULL CHECK(trigger IN ('manual','schedule')),
  profile_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('queued','running','retry','candidate','done','skipped','failed','cancelled','stale')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  revision INTEGER NOT NULL DEFAULT 1,
  max_calls INTEGER NOT NULL CHECK(max_calls > 0),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
  coverage_before INTEGER NOT NULL CHECK(coverage_before >= 0),
  outcome TEXT,
  error_code TEXT,
  gate_bytes INTEGER,
  source_units INTEGER,
  source_version TEXT,
  batch_version INTEGER,
  usage TEXT,
  policy TEXT NOT NULL DEFAULT '{}',
  snapshot_meta TEXT,
  result_refs TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_token TEXT,
  lease_until REAL,
  UNIQUE(agent_id,request_id)
);
CREATE UNIQUE INDEX reflection_jobs_live
  ON reflection_jobs(agent_id) WHERE status IN ('queued','running','retry');
CREATE INDEX reflection_jobs_claim ON reflection_jobs(status,lease_until);
CREATE INDEX reflection_jobs_agent ON reflection_jobs(agent_id,created_at);

-- One immutable copy of every source unit selected for a job. Publication and
-- the final source comparison use these hashes, never a fresh whole history
-- scan inside the write transaction.
CREATE TABLE reflection_snapshots (
  job_id TEXT NOT NULL REFERENCES reflection_jobs(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  occurred_at TEXT NOT NULL,
  byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
  text TEXT NOT NULL,
  coverage TEXT NOT NULL DEFAULT 'retained',
  PRIMARY KEY(job_id,source_id),
  UNIQUE(job_id,ordinal)
);

CREATE TABLE reflection_drafts (
  job_id TEXT NOT NULL REFERENCES reflection_jobs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  source_ids TEXT NOT NULL CHECK(json_valid(source_ids)),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  valid INTEGER NOT NULL DEFAULT 0 CHECK(valid IN (0,1)),
  error_code TEXT,
  PRIMARY KEY(job_id,ordinal)
);

CREATE TABLE reflection_calls (
  job_id TEXT NOT NULL REFERENCES reflection_jobs(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL CHECK(attempt >= 1),
  call_index INTEGER NOT NULL CHECK(call_index >= 1),
  stage TEXT NOT NULL,
  input_bytes INTEGER NOT NULL CHECK(input_bytes >= 0),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT NOT NULL,
  error_code TEXT,
  usage TEXT,
  utc_day TEXT NOT NULL,
  output_bytes INTEGER NOT NULL DEFAULT 0,
  result TEXT,
  PRIMARY KEY(job_id,attempt,call_index)
);
CREATE INDEX reflection_calls_job ON reflection_calls(job_id,call_index);

CREATE TABLE reflection_budget (
  agent_id TEXT NOT NULL REFERENCES agents(id),
  purpose TEXT NOT NULL,
  utc_day TEXT NOT NULL,
  reserved_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_calls >= 0),
  settled_calls INTEGER NOT NULL DEFAULT 0 CHECK(settled_calls >= 0),
  input_bytes INTEGER NOT NULL DEFAULT 0 CHECK(input_bytes >= 0),
  output_bytes INTEGER NOT NULL DEFAULT 0 CHECK(output_bytes >= 0),
  usage TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(agent_id,purpose,utc_day)
);

CREATE TABLE reflection_audit (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  job_id TEXT,
  action TEXT NOT NULL,
  code TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX reflection_audit_agent ON reflection_audit(agent_id,created_at);

CREATE TABLE reflection_qualifications (
 agent_id TEXT NOT NULL REFERENCES agents(id),
 profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 revision INTEGER NOT NULL,
 profile_sha256 TEXT NOT NULL,
 qualified INTEGER NOT NULL CHECK(qualified IN (0,1)),
 sample_count INTEGER NOT NULL,
 acceptable_count INTEGER NOT NULL,
 critical_errors INTEGER NOT NULL,
 evidence TEXT NOT NULL,
 actor TEXT NOT NULL DEFAULT 'owner',
 updated_at TEXT NOT NULL,
 PRIMARY KEY(agent_id,profile_id)
);

-- Indexed invalidation epochs for coverage and immutable safe assets. Normal
-- transcript append is intentionally absent; A maintains its prefix epoch.
CREATE TABLE reflection_source_epochs(agent_id TEXT PRIMARY KEY,epoch INTEGER NOT NULL DEFAULT 0);
INSERT INTO reflection_source_epochs SELECT id,0 FROM agents;
CREATE TRIGGER reflection_agent_created AFTER INSERT ON agents BEGIN
 INSERT INTO reflection_source_epochs VALUES(NEW.id,0);
END;
CREATE TRIGGER reflection_coverage_created AFTER INSERT ON continuity_experiences BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=NEW.agent_id;
END;
CREATE TRIGGER reflection_coverage_changed AFTER UPDATE ON continuity_experiences BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=NEW.agent_id;
END;
CREATE TRIGGER reflection_coverage_deleted AFTER DELETE ON continuity_experiences BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=OLD.agent_id;
END;
CREATE TRIGGER reflection_record_changed AFTER UPDATE ON work_event_records BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=NEW.agent_id;
END;
CREATE TRIGGER reflection_record_deleted AFTER DELETE ON work_event_records BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=OLD.agent_id;
END;
CREATE TRIGGER reflection_chunk_changed AFTER UPDATE ON work_event_chunks BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=(SELECT agent_id FROM work_event_records WHERE id=OLD.record_id);
END;
CREATE TRIGGER reflection_chunk_deleted AFTER DELETE ON work_event_chunks BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=(SELECT agent_id FROM work_event_records WHERE id=OLD.record_id);
END;
CREATE TRIGGER reflection_asset_changed AFTER UPDATE ON attachments BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=NEW.agent_id;
END;
CREATE TRIGGER reflection_asset_deleted AFTER DELETE ON attachments BEGIN
 UPDATE reflection_source_epochs SET epoch=epoch+1 WHERE agent_id=OLD.agent_id;
END;
