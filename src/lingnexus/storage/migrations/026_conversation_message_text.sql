-- A 阶段：对话消息自持文本，运行记录只描述本轮实际发送与截断情况。
--
-- 消息文本改由 conversation_messages.text 直接保存，不再经过 sources/经历库；
-- 分支选择只由 conversations.head_id 决定，未选中分支不再标记来源不可见。
-- chat_runs 去掉来源与自我版本引用，只保留 user_message_id/profile_id/时间戳/
-- context_manifest。
--
-- SQLite 不能删除参与 FOREIGN KEY 的列，且这里还要从 source_revisions 回填文本，
-- 因此两张表都要重建，按官方 ALTER TABLE 流程在关闭外键下进行。迁移运行器用
-- BEGIN IMMEDIATE 包裹脚本，而外键开关在事务内无效，故先结束该事务、关闭外键，
-- 重建完成后重新打开外键并新开一个事务，让运行器继续写入 schema_migrations。
COMMIT;
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

-- 1. 先加文本列，再从原来源表把老库已有的消息文本回填进来，保证老数据可读。
ALTER TABLE conversation_messages ADD COLUMN text TEXT;
UPDATE conversation_messages
 SET text = COALESCE((
   SELECT r.content FROM source_revisions r
   WHERE r.source_id = conversation_messages.source_id
     AND r.revision = conversation_messages.source_revision
 ), '');

-- 附件链接触发器引用 conversation_messages；重建期间它会因表暂时缺失而无法重解析，
-- 故先摘除，等新表就位后按 020 的原定义恢复（校验逻辑不变）。
DROP TRIGGER IF EXISTS conversation_attachments_valid_insert;

CREATE TABLE conversation_messages_new (
 id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(id),
 parent_id TEXT REFERENCES conversation_messages(id),
 role TEXT NOT NULL,
 request_id TEXT NOT NULL,
 text TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(conversation_id,request_id)
);
INSERT INTO conversation_messages_new(id,conversation_id,parent_id,role,request_id,text,created_at)
 SELECT id,conversation_id,parent_id,role,request_id,text,created_at FROM conversation_messages;
DROP TABLE conversation_messages;
ALTER TABLE conversation_messages_new RENAME TO conversation_messages;

CREATE TRIGGER conversation_attachments_valid_insert BEFORE INSERT ON conversation_attachments
WHEN NOT EXISTS (
 SELECT 1 FROM conversation_messages m
 JOIN conversations c ON c.id=m.conversation_id
 JOIN attachments a ON a.id=NEW.attachment_id
 WHERE m.id=NEW.message_id AND m.role='user' AND c.id=NEW.conversation_id
 AND c.agent_id=a.agent_id AND a.sha256=NEW.sha256
)
BEGIN SELECT RAISE(ABORT, 'Invalid user attachment link'); END;

-- 2. 运行记录去掉来源引用、自我版本与 context_sources，只留一份简化清单。
CREATE TABLE chat_runs_new (
 id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES agents(id),
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 profile_id TEXT NOT NULL REFERENCES model_profiles(id),
 status TEXT NOT NULL CHECK(status IN (
   'running','completed','failed','cancelled','interrupted'
 )),
 conversation_id TEXT REFERENCES conversations(id),
 user_message_id TEXT REFERENCES conversation_messages(id),
 context_manifest TEXT,
 created_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(agent_id,request_id)
);
INSERT INTO chat_runs_new(
 id,agent_id,request_id,fingerprint,profile_id,status,
 conversation_id,user_message_id,context_manifest,created_at,finished_at)
 SELECT id,agent_id,request_id,fingerprint,profile_id,status,
   conversation_id,user_message_id,context_manifest,created_at,finished_at
 FROM chat_runs;
DROP TABLE chat_runs;
ALTER TABLE chat_runs_new RENAME TO chat_runs;
CREATE UNIQUE INDEX chat_one_running_per_agent ON chat_runs(agent_id) WHERE status='running';
CREATE INDEX chat_runs_conversation ON chat_runs(conversation_id);

COMMIT;
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
