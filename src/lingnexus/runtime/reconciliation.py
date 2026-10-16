"""Owner observations never turn unknown effects into proved success."""

import hashlib
import json

from lingnexus.common.errors import Conflict
from lingnexus.common.types import utc_now


class RecoveryReview:
    def __init__(self, app, chat, tasks):
        self.app, self.chat, self.tasks = app, chat, tasks

    def _events(self, run_id, kind):
        with self.app.database.transaction() as db:
            if kind == "chat":
                rows = db.execute("SELECT json_extract(data,'$.review') FROM chat_events WHERE run_id=? "
                                  "AND event='notice' AND json_extract(data,'$.code')='owner_recovery_review' "
                                  "ORDER BY sequence DESC LIMIT 20", (run_id,))
            else:
                rows = db.execute("SELECT data FROM task_events WHERE task_id=? AND type='recovery_review' "
                                  "ORDER BY id DESC LIMIT 20", (run_id,))
            return [json.loads(row[0]) for row in rows]

    def view(self, run_id, kind):
        with self.app.sandbox.sessions.observation_guard():
            return self._view(run_id, kind)

    def _view(self, run_id, kind):
        from lingnexus.common.workspace_scan import policy

        runtime = self.chat if kind == "chat" else self.tasks
        run = runtime.get_run(run_id) if kind == "chat" else runtime.get(run_id)
        sessions = self.app.sandbox.run_sessions(run["agent_id"], run_id, kind)
        with self.app.database.transaction() as db:
            if kind == "chat":
                effects = [dict(row) for row in db.execute(
                    "SELECT step,call_id,status FROM chat_tool_effects WHERE run_id=? AND status='executing'",
                    (run_id,))]
            else:
                effects = [dict(row) for row in db.execute(
                    "SELECT id,status FROM task_steps WHERE task_id=? AND status IN ('executing','unknown')",
                    (run_id,))]
        reviews = self._events(run_id, kind)
        data = {"run_id": run_id, "kind": kind, "status": run["status"],
                "workspace_policy": policy(),
                "sessions": sessions, "uncertain_effects": effects, "reviews": reviews,
                "can_abandon": run["status"] in {"blocked", "interrupted", "failed", "paused", "waiting_owner"},
                "guidance": "核对记录不证明命令已停止，不解除未知副作用的阻断。无法确认时请放弃旧运行，"
                            "核对原目录后在独立副本重新开始；任务文件可通过原始基线和成果下载入口检查。"}
        data["version"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return data

    def record(self, run_id, kind, *, request_id, expected_version, conclusion, note):
        with self.app.sandbox.sessions.observation_guard(), self.app.database.transaction() as db:
            # Query all request IDs, not only the bounded display history.
            if kind == "chat":
                rows = db.execute("SELECT json_extract(data,'$.review') FROM chat_events WHERE run_id=? "
                                  "AND event='notice' AND json_extract(data,'$.code')='owner_recovery_review'", (run_id,))
            else:
                rows = db.execute("SELECT data FROM task_events WHERE task_id=? AND type='recovery_review'", (run_id,))
            for row in rows:
                old = json.loads(row[0])
                if old["request_id"] == request_id:
                    if old["conclusion"] != conclusion or old["note"] != note:
                        raise Conflict("核对请求编号已用于其他内容。")
                    return self.view(run_id, kind)
            view = self.view(run_id, kind)
            if view["version"] != expected_version:
                raise Conflict("运行状态已改变，请刷新后重新核对。")
            if view["status"] in {"running", "queued"}:
                raise Conflict("请先停止运行，再记录核对结果。")
            record = {"request_id": request_id, "conclusion": conclusion, "note": note,
                      "observed_version": expected_version, "actor": "owner", "created_at": utc_now(),
                      "cleanup_confirmed": False}
            if conclusion == "abandon":
                if not view["can_abandon"]:
                    raise Conflict("此运行无需或无法放弃。")
                if kind == "chat":
                    db.execute("UPDATE chat_runs SET status='cancelled',finished_at=? WHERE id=?",
                               (utc_now(), run_id))
                else:
                    self.tasks.store.status(run_id, "cancelled", "owner_abandoned")
            if kind == "chat":
                self.chat.store.append(run_id, "notice", {"run_id": run_id,
                    "code": "owner_recovery_review", "message": "已保存人工核对记录。", "review": record})
            else:
                self.tasks.store.event(run_id, "recovery_review", record)
            return self.view(run_id, kind)
