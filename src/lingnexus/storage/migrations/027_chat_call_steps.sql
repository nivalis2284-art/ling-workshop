-- A chat turn is a multi-step loop: every model call (step) must be accounted,
-- not just the first. Rebuild the per-call table so one run can hold one row per
-- (step, attempt) instead of at most two rows per run. Usage stays the latest
-- cumulative snapshot for THAT call, never a sum of snapshots.
CREATE TABLE chat_call_attempts_new (
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 step INTEGER NOT NULL,
 attempt INTEGER NOT NULL CHECK(attempt >= 1),
 profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 status TEXT NOT NULL CHECK(status IN (
   'running','completed','failed','cancelled','interrupted'
 )),
 error_code TEXT, http_status INTEGER, usage TEXT,
 started_at TEXT NOT NULL, finished_at TEXT,
 PRIMARY KEY(run_id,step,attempt)
);

-- Legacy rows only ever held the first call's attempts; keep them as step 1.
INSERT INTO chat_call_attempts_new(
 run_id,step,attempt,profile_id,status,error_code,http_status,usage,started_at,finished_at
)
SELECT run_id,1,attempt,profile_id,status,error_code,http_status,usage,started_at,finished_at
FROM chat_call_attempts;

DROP TABLE chat_call_attempts;
ALTER TABLE chat_call_attempts_new RENAME TO chat_call_attempts;
