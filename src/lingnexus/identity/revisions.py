import json

from lingnexus.common.errors import NotFound, RevisionConflict, SourceInvalid
from lingnexus.common.types import SourceRef, utc_now
from lingnexus.identity.policy import validate_experience


def capture_origins(db, agent_id: str, revision: int) -> None:
    db.execute(
        "INSERT INTO self_revision_origins SELECT agent_id,?,entry_key,proposal_id FROM self_entry_origins WHERE agent_id=?",
        (revision, agent_id),
    )


class Revisions:
    def __init__(self, database, history, current):
        self.database, self.history, self.current = database, history, current

    def list(self, agent_id: str) -> list[dict]:
        with self.database.transaction() as db:
            return [
                dict(row) | {"entries": json.loads(row["entries"])}
                for row in db.execute(
                    "SELECT * FROM self_revisions WHERE agent_id=? ORDER BY revision DESC",
                    (agent_id,),
                )
            ]

    def rollback(self, agent_id: str, *, target_revision: int, expected_revision: int):
        with self.database.transaction() as db:
            current = self.current(agent_id)
            if current.revision != expected_revision:
                raise RevisionConflict("当前版本已变化")
            target = db.execute(
                "SELECT * FROM self_revisions WHERE agent_id=? AND revision=?",
                (agent_id, target_revision),
            ).fetchone()
            if target is None:
                raise NotFound("指定的状态版本不存在")
            origins = db.execute(
                "SELECT * FROM self_revision_origins WHERE agent_id=? AND revision=?",
                (agent_id, target_revision),
            ).fetchall()
            entries = json.loads(target["entries"])
            if set(entries) != {row["entry_key"] for row in origins}:
                raise SourceInvalid("旧版本缺少完整来源，需要重新审阅")
            for row in origins:
                refs = [
                    SourceRef(id=r[0], revision=r[1])
                    for r in db.execute(
                        "SELECT source_id,source_revision FROM proposal_sources WHERE proposal_id=?",
                        (row["proposal_id"],),
                    )
                ]
                validate_experience(self.history, agent_id, refs)
            db.execute(
                "INSERT INTO self_revisions VALUES (?,?,?,?,?)",
                (
                    agent_id,
                    current.revision + 1,
                    target["entries"],
                    f"owner_rollback:{target_revision}",
                    utc_now(),
                ),
            )
            db.execute("DELETE FROM self_entry_origins WHERE agent_id=?", (agent_id,))
            db.executemany(
                "INSERT INTO self_entry_origins VALUES (?,?,?)",
                [(agent_id, row["entry_key"], row["proposal_id"]) for row in origins],
            )
            capture_origins(db, agent_id, current.revision + 1)
            return self.current(agent_id)
