"""Runtime-owned durable records; conversation text lives in the conversation tree."""

import json

from lingnexus.common.errors import DomainError, NotFound
from lingnexus.common.types import utc_now
from lingnexus.storage.db import Database

MAX_EVENT_CURSOR = 2**63 - 1


class RunStore:
    def __init__(self, database: Database):
        self.database = database

    def get(self, run_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise NotFound("Chat run does not exist.")
            return {
                "id": row["id"],
                "agent_id": row["agent_id"],
                "request_id": row["request_id"],
                "profile_id": row["profile_id"],
                "conversation_id": row["conversation_id"],
                "user_message_id": row["user_message_id"],
                "status": row["status"],
                "context_manifest": (
                    json.loads(row["context_manifest"]) if row["context_manifest"] else None
                ),
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }

    def append(self, run_id: str, event: str, data: dict) -> None:
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO chat_events(run_id,sequence,event,data,created_at) "
                "SELECT ?,COALESCE(MAX(sequence),0)+1,?,?,? FROM chat_events WHERE run_id=?",
                (run_id, event, json.dumps(data, ensure_ascii=False), utc_now(), run_id),
            )

    def read_events(self, run_id: str, after: int) -> tuple[list[dict], bool]:
        if not 0 <= after <= MAX_EVENT_CURSOR:
            raise DomainError(f"The event cursor must be between 0 and {MAX_EVENT_CURSOR}.")
        # Read events and terminal status in one snapshot to avoid losing a final
        # event committed between the two reads. Page large replay streams.
        with self.database.transaction() as db:
            run = self.get(run_id)
            rows = db.execute(
                "SELECT sequence,event,data FROM chat_events "
                "WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 256",
                (run_id, after),
            ).fetchall()
            return [
                {"id": r["sequence"], "event": r["event"], "data": json.loads(r["data"])}
                for r in rows
            ], run["status"] != "running"
