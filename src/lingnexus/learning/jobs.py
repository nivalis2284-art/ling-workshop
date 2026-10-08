"""Short durable claims; never reclaim running jobs or replay paid calls implicitly."""

import json
from typing import Any

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.common.types import utc_now
from lingnexus.storage.db import Database


class Jobs:
    def __init__(self, database: Database):
        self.database = database

    def claim(self, agent_id: str | None) -> dict | None:
        with self.database.transaction() as db:
            sql = "SELECT * FROM jobs WHERE kind='consolidate' AND status='queued'"
            args = []
            if agent_id is not None:
                sql += " AND agent_id=?"
                args.append(agent_id)
            row = db.execute(sql + " ORDER BY created_at,rowid LIMIT 1", args).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            db.execute(
                "INSERT INTO learning_job_state(job_id,claimed_at) VALUES (?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET claimed_at=excluded.claimed_at,"
                "finished_at=NULL,outcome=NULL,error_code=NULL,error_meta=NULL,usage=NULL,"
                "result_refs=NULL",
                (row["id"], utc_now()),
            )
            return dict(row)

    def begin_attempt(self, job_id: str, refs: list[dict]) -> None:
        with self.database.transaction() as db:
            db.execute(
                "UPDATE learning_job_state SET attempts=attempts+1 WHERE job_id=?",
                (job_id,),
            )
            db.execute(
                "INSERT INTO learning_attempts(job_id,attempt,started_at,input_refs) "
                "SELECT job_id,attempts,?,? FROM learning_job_state WHERE job_id=?",
                (utc_now(), json.dumps(refs), job_id),
            )

    def finish(
        self,
        job_id: str,
        status: str,
        outcome: str,
        *,
        error_code: str | None = None,
        usage: dict | None = None,
        result_refs: dict | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction() as db:
            now = utc_now()
            # Blocked configuration does not consume the queued job or an attempt.
            stored_status = "queued" if status == "blocked_credentials" else status
            db.execute("UPDATE jobs SET status=? WHERE id=?", (stored_status, job_id))
            db.execute(
                "UPDATE learning_job_state SET finished_at=?,outcome=?,error_code=?,"
                "usage=?,result_refs=? WHERE job_id=?",
                (
                    None if stored_status == "queued" else now,
                    outcome,
                    error_code,
                    json.dumps(usage) if usage is not None else None,
                    json.dumps(result_refs) if result_refs is not None else None,
                    job_id,
                ),
            )
            db.execute(
                "UPDATE learning_attempts SET finished_at=?,outcome=?,error_code=?,usage=? "
                "WHERE job_id=? AND finished_at IS NULL",
                (
                    now,
                    outcome,
                    error_code,
                    json.dumps(usage) if usage is not None else None,
                    job_id,
                ),
            )
            attempts = db.execute(
                "SELECT attempts FROM learning_job_state WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        return {
            "job_id": job_id,
            "status": status,
            "outcome": outcome,
            "attempts": attempts,
            "error_code": error_code,
            "result_refs": result_refs,
        }

    def retry(self, job_id: str) -> dict:
        """Explicit retry authorizes another paid call; terminal successes cannot be replayed."""
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE id=? AND kind='consolidate'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise NotFound("Consolidation job does not exist")
            if row["status"] != "failed":
                raise Conflict("Only a failed consolidation job may be retried")
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
        return {"job_id": job_id, "status": "queued"}
