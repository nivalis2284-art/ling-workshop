CREATE TABLE self_revision_origins (
 agent_id TEXT NOT NULL, revision INTEGER NOT NULL, entry_key TEXT NOT NULL,
 proposal_id TEXT NOT NULL REFERENCES proposals(id), PRIMARY KEY(agent_id,revision,entry_key),
 FOREIGN KEY(agent_id,revision) REFERENCES self_revisions(agent_id,revision)
);
INSERT INTO self_revision_origins SELECT o.agent_id,r.revision,o.entry_key,o.proposal_id
 FROM self_entry_origins o JOIN self_revisions r ON r.agent_id=o.agent_id
 WHERE r.revision=(SELECT max(revision) FROM self_revisions WHERE agent_id=o.agent_id);
