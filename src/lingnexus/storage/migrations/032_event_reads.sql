-- M2 P3: the read ledger behind one agent run's work-event recall.
--
-- Metadata only. A row says which event revision and which source page a run
-- read, in which cursor range and requested page size, how many bytes the page
-- returned, how often the same page was asked for, and when. Original text is
-- never copied here: it stays in the source it is read from (message, safe
-- semantic record, task event), so this table can never become a second,
-- unfiltered copy of history.
--
-- ``byte_count`` is what the run was charged for that fragment: the page of
-- original text for kind 'record', the returned JSON metadata page for kind
-- 'directory'. The uniqueness key is exactly "the same request" (run, kind,
-- event revision, source, cursor, requested size), so an identical repeat is
-- answered with the same fragment and only bumps ``hits`` instead of being
-- charged again — the per-run budget (32 KiB of body, 8 pages) counts distinct
-- fragments of one event revision only.
--
-- A later turn uses this table to describe a finished read-only turn as a
-- receipt (which events and pages were consulted) without replaying the text.

CREATE TABLE work_event_reads (
 id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 agent_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL,
 event_id TEXT NOT NULL,
 event_revision INTEGER NOT NULL CHECK(event_revision >= 1),
 kind TEXT NOT NULL CHECK(kind IN ('directory','record')),
 source_id TEXT NOT NULL DEFAULT '',
 cursor TEXT NOT NULL DEFAULT '',
 next_cursor TEXT,
 sha256 TEXT,
 byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
 page_size INTEGER NOT NULL DEFAULT 0 CHECK(page_size >= 0),
 model_step INTEGER NOT NULL DEFAULT 1 CHECK(model_step >= 1),
 envelope_bytes INTEGER NOT NULL DEFAULT 0 CHECK(envelope_bytes >= 0),
 hits INTEGER NOT NULL DEFAULT 1 CHECK(hits >= 1),
 created_at TEXT NOT NULL,
 UNIQUE(run_id,kind,event_id,event_revision,source_id,cursor,page_size)
);
CREATE INDEX work_event_reads_run ON work_event_reads(run_id,created_at);
CREATE INDEX work_event_reads_event ON work_event_reads(event_id,event_revision);
