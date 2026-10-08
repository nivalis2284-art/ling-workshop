-- Old runs remain replayable without inventing conversation membership.
ALTER TABLE chat_runs ADD COLUMN conversation_id TEXT REFERENCES conversations(id);
ALTER TABLE chat_runs ADD COLUMN user_message_id TEXT REFERENCES conversation_messages(id);
ALTER TABLE chat_runs ADD COLUMN self_revision INTEGER;
CREATE INDEX chat_runs_conversation ON chat_runs(conversation_id);
