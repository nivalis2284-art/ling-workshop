"""Private native checkpoints and durable fences around tool side effects."""

import hashlib
import json

from lingnexus.common.errors import Conflict
from lingnexus.common.types import utc_now


class CheckpointLimit(Conflict):
    """Semantic capture and side-effect fences remain valid; native replay is full."""


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
            raise CheckpointLimit("运行检查点超过大小上限。")
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

    def unresolved(self, run_id: str) -> bool:
        with self.database.transaction() as db:
            return db.execute("SELECT 1 FROM chat_tool_effects WHERE run_id=? AND (status!='completed' OR json_extract(result,'$[1]')='unknown') LIMIT 1",
                              (run_id,)).fetchone() is not None

    def activate_epoch(self, run_id, state, *, expected_state, sealed_state=None, secret=None):
        """Seal the committed old snapshot and atomically CAS the active state.

        No candidate or over-limit snapshot is written before every cap and
        comparison passes. Crashes observe either the whole old or whole new era.
        """
        from .compaction import MAX_ARCHIVE_BYTES, MAX_CHECKPOINT_BYTES, MAX_EPOCHS

        encoded = json.dumps(state, ensure_ascii=False)
        if len(encoded.encode()) > MAX_CHECKPOINT_BYTES:
            raise CheckpointLimit("运行检查点超过大小上限。")
        if secret and secret in encoded:
            raise Conflict("运行检查点包含凭据，已拒绝保存。")
        sealed = json.dumps(sealed_state, ensure_ascii=False) if sealed_state is not None else expected_state
        if sealed is None or len(sealed.encode()) > MAX_CHECKPOINT_BYTES:
            raise CheckpointLimit("运行检查点超过大小上限。")
        if secret and secret in sealed:
            raise Conflict("运行检查点包含凭据，已拒绝保存。")
        with self.database.transaction() as db:
            row = db.execute("SELECT state FROM chat_run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
            old = row[0] if row else None
            if old != expected_state:
                raise Conflict("上下文代际已被其他执行者改变。")
            if old is None:
                raise Conflict("缺少封存起点。")
            prior = json.loads(old)
            epoch = prior.get("context_epoch", 0)
            if state.get("context_epoch") != epoch + 1 or state["step"] < prior["step"]:
                raise Conflict("上下文代际或全局步骤无效。")
            count, total = db.execute("SELECT count(*),coalesce(sum(length(CAST(state AS BLOB))),0) FROM chat_context_epochs WHERE run_id=?", (run_id,)).fetchone()
            if count >= MAX_EPOCHS or total + len(sealed.encode()) > MAX_ARCHIVE_BYTES:
                raise CheckpointLimit("封存运行检查点超过大小上限。")
            if self.unresolved(run_id):
                raise Conflict("副作用未闭合，不能激活代际。")
            db.execute("INSERT INTO chat_context_epochs VALUES(?,?,?,?,?)", (run_id, epoch, sealed, hashlib.sha256(sealed.encode()).hexdigest(), utc_now()))
            self.save(run_id, state, secret=secret)

    def encoded(self, run_id):
        with self.database.transaction() as db:
            row = db.execute("SELECT state FROM chat_run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
            return row[0] if row else None

    def completed_items(self, message_id: str, profile: dict, *, source_guards: dict | None = None) -> list | None:
        with self.database.transaction() as db:
            rows = db.execute("SELECT c.state,r.user_message_id FROM chat_run_checkpoints c JOIN chat_runs r ON r.id=c.run_id "
                              "WHERE r.status='completed' AND c.assistant_message_id=? ORDER BY r.finished_at DESC",
                              (message_id,)).fetchall()
            for entry in rows:
                value = json.loads(entry[0])
                if source_guards is not None and (
                    not value.get("input_guard")
                    or source_guards.get(entry["user_message_id"]) != value["input_guard"]
                ):
                    continue
                if value.get("assistant_message_id") == message_id and value.get("profile") == profile:
                    payload = value["payload"]
                    key = "input" if "input" in payload else "messages"
                    return [*value.get("replay_prefix", []), *payload[key][value["initial_length"]:]]
        return None
