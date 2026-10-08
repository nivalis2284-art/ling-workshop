-- An immutable owner intent and a durable, non-replayable execution fence.
CREATE TABLE dependency_plans (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 plan TEXT NOT NULL, intent_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','success','failed','unknown')),
 decision TEXT CHECK(decision IN ('approve','reject')),
 result TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(task_id,request_id)
);
CREATE INDEX dependency_plans_task ON dependency_plans(task_id,status,created_at);
CREATE TRIGGER dependency_plan_immutable
BEFORE UPDATE OF id,task_id,request_id,fingerprint,plan,intent_hash,created_at
ON dependency_plans
BEGIN
 SELECT RAISE(ABORT, 'dependency plan is immutable');
END;
