-- Private replay material; never part of the owner-facing event preview API.
CREATE TABLE chat_run_checkpoints (
 run_id TEXT PRIMARY KEY REFERENCES chat_runs(id),
 state TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 assistant_message_id TEXT REFERENCES conversation_messages(id) ON DELETE SET NULL
);
CREATE INDEX chat_checkpoint_message ON chat_run_checkpoints(assistant_message_id);
CREATE TABLE chat_tool_effects (
 run_id TEXT NOT NULL REFERENCES chat_runs(id),
 step INTEGER NOT NULL,
 call_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('executing','completed')),
 result TEXT,
 PRIMARY KEY(run_id,step,call_id)
);
