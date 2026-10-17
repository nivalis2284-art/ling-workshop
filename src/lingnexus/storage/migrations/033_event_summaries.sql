-- P4: durable, bounded event-summary jobs (M2 phase one).
--
-- One row per requested summary, deduplicated per event/revision/policy so a
-- repeated enqueue cannot fan out paid calls, while terminal rows stay as
-- history. Leases are numeric unix seconds: string comparison of ISO stamps
-- with and without microseconds is not safe for expiry checks.

CREATE TABLE event_summary_jobs (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  event_revision INTEGER NOT NULL,
  policy_version TEXT NOT NULL,
  profile_id TEXT,
  max_calls INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  coverage TEXT,
  outcome TEXT,
  error_code TEXT,
  usage TEXT,
  result_revision INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_until REAL,
  lease_token TEXT
);

-- Only one live job may exist for the same event revision and policy; finished
-- history is kept for audit instead of being overwritten.
CREATE UNIQUE INDEX event_summary_jobs_active
  ON event_summary_jobs (event_id, event_revision, policy_version)
  WHERE status IN ('queued', 'running', 'retry');

CREATE INDEX event_summary_jobs_claim ON event_summary_jobs (status, lease_until);
CREATE INDEX event_summary_jobs_event ON event_summary_jobs (event_id, created_at);

-- One row per model request: every call's stage and usage stays inspectable,
-- including calls made before a restart.
CREATE TABLE event_summary_calls (
  job_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  call_index INTEGER NOT NULL,
  stage TEXT NOT NULL,
  input_bytes INTEGER,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  error_code TEXT,
  usage TEXT,
  PRIMARY KEY (job_id, attempt, call_index)
);
