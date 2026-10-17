-- M2 v2 R1/R2: one server-owned main per individual, independent work
-- conversations, durable return outbox, and a shared experience/source ledger.
-- This is additive. Existing conversation trees and work_event tables are kept
-- as the authoritative original text; no private checkpoint is copied here.

CREATE TABLE continuity_main_conversations (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Lifecycle revision, result revision and publication identity are deliberately
-- separate. A transition back to pending_review must not duplicate a return.
CREATE TABLE continuity_work_branches (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
    main_conversation_id TEXT NOT NULL REFERENCES conversations(id),
    origin_message_id TEXT,
    objective TEXT NOT NULL,
    profile_id TEXT REFERENCES model_profiles(id),
    status TEXT NOT NULL CHECK(status IN (
        'queued','starting','running','awaiting_approval','paused','interrupted',
        'pending_review','finishing','archived','failed','cancelled','unknown'
    )),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    finish_requested INTEGER NOT NULL DEFAULT 0 CHECK(finish_requested IN (0,1)),
    run_id TEXT REFERENCES chat_runs(id),
    source_anchor_id TEXT,
    members TEXT NOT NULL DEFAULT '[]',
    members_digest TEXT,
    background TEXT NOT NULL DEFAULT '[]',
    background_digest TEXT,
    return_report TEXT,
    grants TEXT NOT NULL DEFAULT '{}',
    outcome TEXT,
    error_code TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'none'
        CHECK(delivery_state IN ('none','pending','unknown','delivered','blocked')),
    archive_id TEXT,
    archive_version INTEGER,
    result_revision INTEGER NOT NULL DEFAULT 0 CHECK(result_revision >= 0),
    return_state TEXT NOT NULL DEFAULT 'none'
        CHECK(return_state IN ('none','pending','blocked','published')),
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id,request_id)
);
CREATE INDEX continuity_work_agent ON continuity_work_branches(agent_id,status,created_at);
CREATE INDEX continuity_work_main ON continuity_work_branches(main_conversation_id,created_at);
CREATE UNIQUE INDEX continuity_one_active_work
    ON continuity_work_branches(agent_id)
    WHERE status IN ('starting','running','awaiting_approval','finishing');

CREATE TABLE continuity_work_guidance (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES continuity_work_branches(id),
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    delivery TEXT NOT NULL DEFAULT 'queued'
        CHECK(delivery IN ('queued','delivered','unknown')),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(branch_id,request_id)
);

-- `result_revision` is the idempotency key for the return. A lifecycle guide or
-- pause may change the work revision without creating another return.
CREATE TABLE continuity_branch_returns (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES continuity_work_branches(id),
    result_revision INTEGER NOT NULL CHECK(result_revision >= 1),
    archive_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    head_id TEXT,
    members_digest TEXT NOT NULL,
    archive_version INTEGER NOT NULL CHECK(archive_version >= 1),
    target_conversation_id TEXT NOT NULL REFERENCES conversations(id),
    target_binding_revision INTEGER NOT NULL,
    anchor_id TEXT,
    report TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready','blocked','published')),
    reason TEXT,
    published_message_id TEXT REFERENCES conversation_messages(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(branch_id,result_revision)
);
CREATE INDEX continuity_returns_ready ON continuity_branch_returns(status,created_at);

CREATE TABLE continuity_experiences (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    kind TEXT NOT NULL CHECK(kind IN ('work_branch','main_event','legacy_work_event')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    occurred_start TEXT,
    occurred_end TEXT,
    coverage TEXT NOT NULL DEFAULT 'exact',
    valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)),
    reason TEXT,
    conversation_id TEXT REFERENCES conversations(id),
    branch_id TEXT REFERENCES continuity_work_branches(id),
    batch_id TEXT,
    policy_version TEXT,
    occurred_precision TEXT NOT NULL DEFAULT 'exact'
        CHECK(occurred_precision IN ('exact','date_only','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX continuity_experiences_agent ON continuity_experiences(agent_id,created_at);
CREATE INDEX continuity_experiences_conversation ON continuity_experiences(conversation_id,kind);

-- A source row is an index over authoritative text, never a second copy. `ref`
-- is opaque to callers and resolved by the Continuity source resolver. revoking
-- coverage changes only this ledger; original messages/records stay intact.
CREATE TABLE continuity_experience_sources (
    experience_id TEXT NOT NULL REFERENCES continuity_experiences(id),
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    byte_count INTEGER,
    sha256 TEXT,
    coverage TEXT NOT NULL DEFAULT 'retained',
    grants TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(experience_id,source_id)
);
CREATE INDEX continuity_source_ref ON continuity_experience_sources(kind,ref);

-- Explicit dependency ledger for actual model inputs. This survives cleanup or
-- loss of a private checkpoint and is included in portable exports by E.
CREATE TABLE continuity_run_sources (
    run_id TEXT NOT NULL REFERENCES chat_runs(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,source_kind,ref)
);

CREATE TABLE continuity_answer_dependencies (
    run_id TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE CASCADE,
    experience_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,assistant_message_id,experience_id)
);
CREATE INDEX continuity_answer_message ON continuity_answer_dependencies(assistant_message_id);

CREATE TABLE continuity_experience_reads (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES chat_runs(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    experience_id TEXT NOT NULL,
    experience_revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    cursor TEXT NOT NULL DEFAULT '',
    next_cursor TEXT,
    sha256 TEXT,
    byte_count INTEGER NOT NULL DEFAULT 0,
    page_bytes INTEGER NOT NULL DEFAULT 0,
    page_size INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    model_step INTEGER NOT NULL DEFAULT 0,
    envelope_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX continuity_reads_run ON continuity_experience_reads(run_id,model_step);

CREATE TABLE continuity_reflection_drafts (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    batch_key TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    units TEXT NOT NULL,
    units_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged','published','discarded')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(agent_id,batch_key)
);

-- Stable-prefix epochs: ordinary append leaves admitted prefixes valid. Owner
-- edits, deletes, ancestry switches and attachment relinking invalidate tokens.
CREATE TABLE continuity_source_versions (
 conversation_id TEXT PRIMARY KEY,
 epoch INTEGER NOT NULL DEFAULT 0
);
INSERT INTO continuity_source_versions SELECT id,0 FROM conversations;
CREATE TABLE continuity_message_assets (
 message_id TEXT PRIMARY KEY,
 revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO continuity_message_assets SELECT id,0 FROM conversation_messages;
CREATE TRIGGER continuity_message_created AFTER INSERT ON conversation_messages BEGIN
 INSERT INTO continuity_message_assets VALUES(NEW.id,0);
END;
CREATE TRIGGER continuity_conversation_created AFTER INSERT ON conversations BEGIN
 INSERT INTO continuity_source_versions VALUES(NEW.id,0);
END;
CREATE TRIGGER continuity_head_changed AFTER UPDATE OF head_id ON conversations
WHEN OLD.head_id IS NOT NEW.head_id AND NOT EXISTS (
 SELECT 1 FROM conversation_messages m WHERE m.id=NEW.head_id
 AND m.conversation_id=NEW.id AND m.parent_id IS OLD.head_id
) BEGIN
 UPDATE continuity_source_versions SET epoch=epoch+1 WHERE conversation_id=NEW.id;
END;
CREATE TRIGGER continuity_message_changed AFTER UPDATE OF text,parent_id,created_at ON conversation_messages
WHEN OLD.text IS NOT NEW.text OR OLD.parent_id IS NOT NEW.parent_id OR OLD.created_at IS NOT NEW.created_at BEGIN
 UPDATE continuity_source_versions SET epoch=epoch+1 WHERE conversation_id=NEW.conversation_id;
END;
CREATE TRIGGER continuity_message_deleted AFTER DELETE ON conversation_messages BEGIN
 UPDATE continuity_source_versions SET epoch=epoch+1 WHERE conversation_id=OLD.conversation_id;
END;
CREATE TRIGGER continuity_attachment_linked AFTER INSERT ON conversation_attachments BEGIN
 UPDATE continuity_message_assets SET revision=revision+1 WHERE message_id=NEW.message_id;
END;
CREATE TRIGGER continuity_attachment_unlinked AFTER DELETE ON conversation_attachments BEGIN
 UPDATE continuity_message_assets SET revision=revision+1 WHERE message_id=OLD.message_id;
END;
CREATE TRIGGER continuity_attachment_relinked AFTER UPDATE ON conversation_attachments BEGIN
 UPDATE continuity_message_assets SET revision=revision+1 WHERE message_id IN (OLD.message_id,NEW.message_id);
END;
CREATE TRIGGER continuity_asset_changed AFTER UPDATE OF extraction,sha256 ON attachments BEGIN
 UPDATE continuity_message_assets SET revision=revision+1 WHERE message_id IN
 (SELECT message_id FROM conversation_attachments WHERE attachment_id=NEW.id);
END;
CREATE TRIGGER continuity_record_changed AFTER UPDATE ON work_event_records BEGIN
 UPDATE continuity_source_versions SET epoch=epoch+1 WHERE conversation_id=NEW.conversation_id;
END;
CREATE TRIGGER continuity_chunk_changed AFTER UPDATE ON work_event_chunks BEGIN
 UPDATE continuity_source_versions SET epoch=epoch+1 WHERE conversation_id IN
 (SELECT conversation_id FROM work_event_records WHERE id=NEW.record_id);
END;
CREATE TRIGGER continuity_chunk_deleted BEFORE DELETE ON work_event_chunks BEGIN
 UPDATE continuity_source_versions SET epoch=epoch+1 WHERE conversation_id IN
 (SELECT conversation_id FROM work_event_records WHERE id=OLD.record_id);
END;
