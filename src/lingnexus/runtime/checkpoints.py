"""Private native checkpoints and durable fences around tool side effects."""

import json

from lingnexus.common.errors import Conflict
from lingnexus.common.types import utc_now


class RunCheckpoints:
    def __init__(self, database):
        self.database = database

    def load(self, run_id: str) -> dict | None:
        with self.database.transaction() as db:
            row = db.execute("SELECT state FROM chat_run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, run_id: str, state: dict, *, secret: str | None = None) -> None:
        encoded = json.dumps(state, ensure_ascii=False)
        if len(encoded.encode()) > 8 * 1_048_576:
            raise Conflict("运行检查点超过大小上限。")
        if secret and secret in encoded:
            raise Conflict("运行检查点包含凭据，已拒绝保存。")
        with self.database.transaction() as db:
            db.execute("INSERT INTO chat_run_checkpoints VALUES(?,?,?,?) "
                       "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at,"
                       "assistant_message_id=excluded.assistant_message_id",
                       (run_id, encoded, utc_now(), state.get("assistant_message_id")))

    def effect(self, run_id: str, step: int, call_id: str) -> dict | None:
        with self.database.transaction() as db:
            row = db.execute("SELECT status,result FROM chat_tool_effects WHERE run_id=? AND step=? AND call_id=?",
                             (run_id, step, call_id)).fetchone()
            return {"status": row[0], "result": json.loads(row[1]) if row[1] else None} if row else None

    def begin(self, run_id: str, step: int, call_id: str) -> None:
        with self.database.transaction() as db:
            db.execute("INSERT INTO chat_tool_effects(run_id,step,call_id,status) VALUES(?,?,?,'executing')",
                       (run_id, step, call_id))

    def finish(self, run_id: str, step: int, call_id: str, result: list) -> None:
        with self.database.transaction() as db:
            db.execute("UPDATE chat_tool_effects SET status='completed',result=? "
                       "WHERE run_id=? AND step=? AND call_id=?",
                       (json.dumps(result, ensure_ascii=False), run_id, step, call_id))

    def uncertain(self, run_id: str) -> bool:
        with self.database.transaction() as db:
            return db.execute("SELECT 1 FROM chat_tool_effects WHERE run_id=? AND status='executing' LIMIT 1",
                              (run_id,)).fetchone() is not None

    def completed_items(self, message_id: str, profile: dict) -> list | None:
        with self.database.transaction() as db:
            rows = db.execute("SELECT c.state FROM chat_run_checkpoints c JOIN chat_runs r ON r.id=c.run_id "
                              "WHERE r.status='completed' AND c.assistant_message_id=? ORDER BY r.finished_at DESC",
                              (message_id,)).fetchall()
            for entry in rows:
                value = json.loads(entry[0])
                if value.get("assistant_message_id") == message_id and value.get("profile") == profile:
                    payload = value["payload"]
                    key = "input" if "input" in payload else "messages"
                    return payload[key][value["initial_length"]:]
        return None
