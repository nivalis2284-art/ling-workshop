-- M2 P1: work-event index, safe semantic records, source list and summaries.
--
-- Layering: raw messages stay in conversation_messages; this module only adds
-- the event index over the selected branch, the filtered tool/guidance records
-- the runtime already decided are safe to keep, the source ledger, and summary
-- versions. Private native checkpoints and chat_tool_effects keep serving native
-- recovery and audit and are never a read surface for these tables.
--
-- Message anchors are plain text on purpose. A work event records a historical
-- span, so it must not block the owner's delete-turns: a deleted anchor simply
-- makes the event stale (revalidated on read) instead of raising a foreign-key
-- error. Same reasoning applies to agent_id/conversation_id here, which are
-- also validated by the service against the live row before use.

CREATE TABLE work_events (
 id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL,
 before_message_id TEXT,
 start_message_id TEXT,
 end_message_id TEXT,
 title TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','closed','invalidated')),
 outcome TEXT,
 folded INTEGER NOT NULL DEFAULT 0 CHECK(folded IN (0,1)),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 grants TEXT NOT NULL,
 summary TEXT,
 coverage TEXT NOT NULL DEFAULT '{}',
 members TEXT NOT NULL DEFAULT '[]',
 run_ids TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL,
 closed_at TEXT,
 UNIQUE(agent_id,request_id)
);
-- One active event per (conversation, branch head position) is enforced by the
-- service under the same transaction; these indexes serve its lookups.
CREATE INDEX work_events_conversation ON work_events(conversation_id,status);
CREATE INDEX work_events_agent ON work_events(agent_id,created_at);

-- Safe, owner-readable semantic text captured after filtering and before the
-- runtime's preview truncation. `grants` is the authorization snapshot the
-- record was admitted under (checkpoint grants when the runtime supplies them,
-- otherwise the current revision) so a later withdrawal never re-labels this
-- text as newly authorized.
CREATE TABLE work_event_records (
 id TEXT PRIMARY KEY,
 run_id TEXT REFERENCES chat_runs(id),
 agent_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 step INTEGER NOT NULL DEFAULT 0 CHECK(step >= 0),
 call_id TEXT NOT NULL DEFAULT '',
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
 metadata TEXT,
 grants TEXT NOT NULL,
 sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
 byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
 created_at TEXT NOT NULL,
 UNIQUE(run_id,kind,step,call_id,attempt)
);
CREATE INDEX work_event_records_event ON work_event_records(conversation_id,created_at);

-- Body text is stored as one or more 64 KiB byte chunks: a large tool result is
-- paged with byte offsets and never read in full to answer one page request.
CREATE TABLE work_event_chunks (
 record_id TEXT NOT NULL REFERENCES work_event_records(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
 body BLOB NOT NULL,
 PRIMARY KEY(record_id,ordinal)
);

-- Event source ledger. `ref` locates the member inside its authoritative store
-- (message id, record id, attachment id, task step id); no path or SQL is ever
-- accepted from a caller. Coverage is retained/bounded/redacted/missing/revoked.
CREATE TABLE work_event_sources (
 event_id TEXT NOT NULL REFERENCES work_events(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 ref TEXT NOT NULL,
 byte_count INTEGER,
 sha256 TEXT,
 coverage TEXT NOT NULL DEFAULT 'retained',
 ordinal INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(event_id,source_id)
);
CREATE INDEX work_event_sources_ref ON work_event_sources(kind,ref);

-- One immutable row per summary revision; the newest approved revision wins and
-- older text is never overwritten. `kind` distinguishes rule skeleton, model
-- candidate and owner-edited text.
CREATE TABLE work_event_summaries (
 event_id TEXT NOT NULL REFERENCES work_events(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL CHECK(revision >= 1),
 kind TEXT NOT NULL,
 text TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(event_id,revision)
);

-- Append-only audit of lifecycle actions; revision is the event revision the
-- action produced or observed, so concurrent writers cannot rewrite history.
CREATE TABLE work_event_audit (
 event_id TEXT NOT NULL REFERENCES work_events(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL CHECK(revision >= 0),
 action TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(event_id,revision)
);

-- Explicit event↔task link, written only by the owner-facing entry that started
-- or returned the task; same-agent/time proximity never auto-links.
CREATE TABLE work_event_task_links (
 event_id TEXT NOT NULL REFERENCES work_events(id) ON DELETE CASCADE,
 task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 agent_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL,
 origin_message_id TEXT,
 return_message_id TEXT,
 grants TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(event_id,task_id)
);
CREATE INDEX work_event_task_links_task ON work_event_task_links(task_id);
