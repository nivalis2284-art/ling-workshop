-- M2 phase one: an existing work branch is the durable task agent. This is
-- additive. The branch keeps its id and work conversation; task-state versions
-- and episodes describe what that same agent currently understands and when it
-- was awake. Existing archives and source epochs are never rewritten here.

CREATE TABLE continuity_work_task_states (
    branch_id TEXT NOT NULL REFERENCES continuity_work_branches(id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    objective TEXT NOT NULL,
    decisions TEXT NOT NULL DEFAULT '[]',
    progress TEXT NOT NULL DEFAULT '[]',
    unfinished TEXT NOT NULL DEFAULT '[]',
    verified TEXT NOT NULL DEFAULT '[]',
    unknowns TEXT NOT NULL DEFAULT '[]',
    artifacts TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    grants TEXT NOT NULL DEFAULT '{}',
    digest TEXT NOT NULL,
    run_id TEXT REFERENCES chat_runs(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(branch_id,revision)
);
CREATE INDEX continuity_task_states_run ON continuity_work_task_states(run_id);

CREATE TABLE continuity_work_episodes (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES continuity_work_branches(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    predecessor_id TEXT REFERENCES continuity_work_episodes(id),
    run_id TEXT REFERENCES chat_runs(id),
    status TEXT NOT NULL CHECK(status IN (
        'active','paused','completed','interrupted','pending_review',
        'cancelled','archived'
    )),
    reason TEXT,
    task_state_revision INTEGER NOT NULL DEFAULT 0 CHECK(task_state_revision >= 0),
    source_epoch INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(branch_id,ordinal),
    UNIQUE(branch_id,run_id)
);
CREATE INDEX continuity_episodes_branch ON continuity_work_episodes(branch_id,ordinal);

-- A run belongs to at most one episode. Existing work runs are attached to a
-- legacy episode without copying their checkpoints or changing their status.
ALTER TABLE continuity_work_branches ADD COLUMN current_episode_id TEXT REFERENCES continuity_work_episodes(id);
ALTER TABLE continuity_work_branches ADD COLUMN task_state_revision INTEGER NOT NULL DEFAULT 0 CHECK(task_state_revision >= 0);

-- Guidance gains actual-consumption evidence. `delivery` remains for legacy
-- readers; `consumed_at` is only set by the runtime after it has injected the
-- text into the native payload at a real step boundary.
ALTER TABLE continuity_work_guidance ADD COLUMN episode_id TEXT REFERENCES continuity_work_episodes(id);
ALTER TABLE continuity_work_guidance ADD COLUMN consumed_at TEXT;

-- Every archived result, not only the latest branch-level archive, belongs to a
-- closed episode. This is a new immutable ledger keyed to the existing archive
-- id so old archives are untouched and future waking has a stable history.
CREATE TABLE continuity_episode_archives (
    archive_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES continuity_work_branches(id),
    episode_id TEXT NOT NULL REFERENCES continuity_work_episodes(id),
    experience_id TEXT NOT NULL,
    return_id TEXT REFERENCES continuity_branch_returns(id),
    outcome TEXT,
    archive_version INTEGER NOT NULL CHECK(archive_version >= 1),
    head_id TEXT,
    members_digest TEXT,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX continuity_episode_archives_branch ON continuity_episode_archives(branch_id,created_at);

-- Compatibility data only. No existing branch is promoted or renamed, and old
-- finished branches keep their archived status until an owner explicitly wakes
-- them. This episode documents the text already present in the conversation.
INSERT INTO continuity_work_episodes(id,branch_id,ordinal,status,opened_at,closed_at)
SELECT 'legacy:' || b.id,b.id,1,
       CASE WHEN b.status IN ('archived','cancelled','failed') THEN 'archived' ELSE 'active' END,
       b.created_at,
       CASE WHEN b.status IN ('archived','cancelled','failed') THEN COALESCE(b.finished_at,b.updated_at) END
FROM continuity_work_branches b;

UPDATE continuity_work_branches
SET current_episode_id='legacy:' || id
WHERE current_episode_id IS NULL;

INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,progress,unfinished,verified,unknowns,artifacts,sources,grants,digest,created_at)
SELECT b.id,1,b.objective,'[]',json_array(json_object('kind','created','detail','旧工作分支已建立独立执行上下文。')),
       CASE WHEN b.status IN ('archived','cancelled','failed') THEN '[]'
            ELSE json_array(json_object('kind','continue','detail','旧工作分支在迁移时尚未完成；需要核对原运行与交付。')) END,
       '[]',json_array(json_object('kind','legacy_migration','detail','迁移未重新核验旧运行事实，只保留原目标。')),
       '[]','[]',b.grants,
       'legacy-unverified',
       b.updated_at
FROM continuity_work_branches b;

-- Every legacy branch has exactly the one task-state revision inserted above.
-- Keep the branch and its legacy episode pointing at that version instead of
-- leaving the default zero as a false "no state" marker.
UPDATE continuity_work_branches SET task_state_revision=1;
UPDATE continuity_work_episodes SET task_state_revision=1 WHERE id LIKE 'legacy:%';

-- Legacy episodes did not retain a run binding when several runs existed on the
-- same conversation. Record only the branch's current run if it truly belongs
-- to the work conversation; otherwise leave it null rather than guess.
UPDATE continuity_work_episodes
SET run_id=(SELECT b.run_id FROM continuity_work_branches b
            JOIN chat_runs r ON r.id=b.run_id
            WHERE b.id=continuity_work_episodes.branch_id
              AND r.conversation_id=b.conversation_id)
WHERE id LIKE 'legacy:%';

INSERT INTO continuity_episode_archives(archive_id,branch_id,episode_id,experience_id,outcome,archive_version,head_id,report,created_at)
SELECT b.archive_id,b.id,b.current_episode_id,'work-live:'||b.id,b.outcome,b.archive_version,
       (SELECT r.head_id FROM continuity_branch_returns r
        WHERE r.branch_id=b.id ORDER BY r.result_revision DESC LIMIT 1),
       COALESCE(b.return_report,''),COALESCE(b.finished_at,b.updated_at)
FROM continuity_work_branches b
WHERE b.archive_id IS NOT NULL AND b.current_episode_id IS NOT NULL;

CREATE TRIGGER continuity_episode_archive_immutable
BEFORE UPDATE ON continuity_episode_archives
BEGIN SELECT RAISE(ABORT,'Episode archive is immutable'); END;
