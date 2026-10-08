CREATE TABLE learning_job_state (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),
 attempts INTEGER NOT NULL DEFAULT 0,
 claimed_at TEXT,
 finished_at TEXT,
 outcome TEXT,
 error_code TEXT,
 error_meta TEXT,
 usage TEXT,
 result_refs TEXT
);
CREATE TABLE learning_attempts (
 job_id TEXT NOT NULL REFERENCES jobs(id),
 attempt INTEGER NOT NULL,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 outcome TEXT,
 error_code TEXT,
 usage TEXT,
 input_refs TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);
CREATE INDEX learning_queued_jobs ON jobs(kind,status,created_at);
