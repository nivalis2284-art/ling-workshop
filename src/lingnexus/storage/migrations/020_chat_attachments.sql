-- Original files belong to a user node, so branch selection and regeneration
-- reuse the same ordered IDs and immutable digest without backfilling uploads.
CREATE TABLE conversation_attachments (
 conversation_id TEXT NOT NULL REFERENCES conversations(id),
 message_id TEXT NOT NULL REFERENCES conversation_messages(id),
 attachment_id TEXT NOT NULL REFERENCES attachments(id),
 ordinal INTEGER NOT NULL CHECK(ordinal >= 0 AND ordinal < 16),
 sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
 PRIMARY KEY(message_id, attachment_id), UNIQUE(message_id, ordinal)
);
CREATE INDEX conversation_attachments_conversation ON conversation_attachments(conversation_id);
CREATE TRIGGER conversation_attachments_valid_insert BEFORE INSERT ON conversation_attachments
WHEN NOT EXISTS (
 SELECT 1 FROM conversation_messages m
 JOIN conversations c ON c.id=m.conversation_id
 JOIN attachments a ON a.id=NEW.attachment_id
 WHERE m.id=NEW.message_id AND m.role='user' AND c.id=NEW.conversation_id
 AND c.agent_id=a.agent_id AND a.sha256=NEW.sha256
)
BEGIN SELECT RAISE(ABORT, 'Invalid user attachment link'); END;
CREATE TRIGGER conversation_attachments_immutable BEFORE UPDATE ON conversation_attachments
BEGIN SELECT RAISE(ABORT, 'User attachment links are immutable'); END;
