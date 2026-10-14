-- Per-conversation access mode: the owner's standing answer for one conversation.
-- Absent row means 'confirm' — every gated call waits for the owner, as always.
-- 'full' means commands and remote (mcp__*) tools run without the per-call
-- confirmation in that conversation only; the sandbox, audit, directory grants
-- and browser click approvals are all unchanged.
CREATE TABLE chat_conversation_access (
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
 mode TEXT NOT NULL CHECK(mode IN ('confirm','full')),
 updated_at TEXT NOT NULL
);
