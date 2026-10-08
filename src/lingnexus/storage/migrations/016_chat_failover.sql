-- CHAT policy/display records never contain credentials or provider continuation.
ALTER TABLE chat_runs ADD COLUMN context_manifest TEXT;

CREATE TABLE chat_conversation_failover (
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
 main_profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 backup_profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 bound_run_id TEXT NOT NULL REFERENCES chat_runs(id),
 created_at TEXT NOT NULL,
 CHECK(main_profile_id <> backup_profile_id)
);

-- Immutable request/initial policy, separate from chat_runs.profile_id (active profile).
CREATE TABLE chat_run_profiles (
 run_id TEXT PRIMARY KEY REFERENCES chat_runs(id),
 requested_profile_id TEXT REFERENCES model_profiles(id),
 initial_profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 main_profile_id TEXT REFERENCES model_profiles(id),
 fallback_profile_id TEXT REFERENCES model_profiles(id),
 selection_reason TEXT NOT NULL CHECK(selection_reason IN (
   'primary','manual','conversation_backup','credentials_failover'
 )),
 conversation_sources TEXT NOT NULL
);

-- Usage is the latest cumulative snapshot for THIS call, never a sum of snapshots.
CREATE TABLE chat_call_attempts (
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 attempt INTEGER NOT NULL CHECK(attempt IN (1,2)),
 profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 status TEXT NOT NULL CHECK(status IN (
   'running','completed','failed','cancelled','interrupted'
 )),
 error_code TEXT, http_status INTEGER, usage TEXT,
 started_at TEXT NOT NULL, finished_at TEXT,
 PRIMARY KEY(run_id,attempt), UNIQUE(run_id,profile_id)
);
