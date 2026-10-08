CREATE TABLE activity_web_grants (
 agent_id TEXT PRIMARY KEY REFERENCES agents(id),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 origins TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE activity_scopes (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 kind TEXT NOT NULL CHECK(kind IN ('file_watch','web_watch')),
 target TEXT NOT NULL, web_origin TEXT,
 revision INTEGER NOT NULL CHECK(revision >= 1),
 status TEXT NOT NULL CHECK(status IN ('authorized','revoked')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(agent_id,kind,target)
);

CREATE TABLE activities (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 kind TEXT NOT NULL CHECK(kind IN ('reminder','file_watch','web_watch','topic_review')),
 title TEXT NOT NULL, target TEXT, origin TEXT NOT NULL CHECK(origin IN ('owner','model')),
 scope_id TEXT REFERENCES activity_scopes(id), revision INTEGER NOT NULL CHECK(revision >= 1),
 status TEXT NOT NULL CHECK(status IN ('active','paused','cancelled','completed')),
 next_due_at TEXT NOT NULL, interval_seconds INTEGER CHECK(interval_seconds >= 60),
 last_hash TEXT, last_outcome TEXT, last_error TEXT, last_checked_at TEXT,
 event_cursor INTEGER NOT NULL DEFAULT 0, trigger_seq INTEGER NOT NULL DEFAULT 0,
 claim_token TEXT, claim_until TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((kind IN ('file_watch','web_watch') AND scope_id IS NOT NULL AND target IS NOT NULL)
    OR (kind IN ('reminder','topic_review') AND scope_id IS NULL AND target IS NULL))
);
CREATE INDEX activities_due ON activities(status,next_due_at);

CREATE TABLE activity_evidence (
 id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id),
 dedupe_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
 source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, observed_at TEXT NOT NULL,
 UNIQUE(activity_id,dedupe_key),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);

CREATE TABLE activity_notifications (
 id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id),
 agent_id TEXT NOT NULL REFERENCES agents(id),
 evidence_id TEXT NOT NULL UNIQUE REFERENCES activity_evidence(id),
 message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX activity_notifications_agent ON activity_notifications(agent_id,created_at);

CREATE TABLE activity_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 agent_id TEXT NOT NULL REFERENCES agents(id), dedupe_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('tool_completed','task_completed')),
 content TEXT NOT NULL, fingerprint TEXT NOT NULL,
 source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
 UNIQUE(agent_id,dedupe_key),
 FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions(source_id,revision)
);
CREATE INDEX activity_events_agent ON activity_events(agent_id,seq);
