"""Versioned work boundaries and authorized, bounded views of retained sources."""

import base64
import hashlib
import json

from lingnexus.common.errors import Conflict, DomainError, NotFound, PermissionDenied
from lingnexus.common.types import new_id, utc_now

from .store import EventRecordStore


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text, size):
    raw = text.encode("utf-8")
    return text if len(raw) <= size else raw[:size].decode("utf-8", errors="ignore") + "…"


def scope_snapshot(app, agent_id):
    browser = getattr(app, "browser", None)
    return {
        "permissions": app.permissions.current(agent_id),
        "browser": browser.current_origins(agent_id) if browser is not None else None,
    }


def _nodes(messages):
    return [{"id": m["id"], "parent_id": m.get("parent_id"), "role": m["role"],
             "sha256": _hash(m["text"]) if "text" in m else m["sha256"], "attachment_ids": m.get("attachment_ids", [])}
            for m in messages]


class WorkEvents:
    def __init__(self, app):
        self.app = app
        self.database = app.database
        self.records = EventRecordStore(app)

    def _row(self, event_id):
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM work_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise NotFound("工作事件不存在。")
            return dict(row)

    def _metadata_path(self, conversation_id):
        """Validate ancestry and body digests without loading the transcript."""
        with self.database.transaction() as db:
            head = self.app.conversations.read(conversation_id)["head_id"]
            nodes, seen = [], set()
            while head:
                if head in seen:
                    raise Conflict("消息引用存在循环。")
                seen.add(head)
                row = db.execute("SELECT rowid,id,parent_id,role FROM conversation_messages WHERE conversation_id=? AND id=?", (conversation_id, head)).fetchone()
                if row is None:
                    raise NotFound("分支来源不存在。")
                digest = hashlib.sha256()
                with db.blobopen("conversation_messages", "text", row["rowid"], readonly=True) as body:
                    for part in iter(lambda: body.read(65536), b""):
                        digest.update(part)
                attachments = [r[0] for r in db.execute("SELECT attachment_id FROM conversation_attachments WHERE message_id=? ORDER BY ordinal", (head,))]
                nodes.append(dict(row) | {"sha256": digest.hexdigest(), "attachment_ids": attachments})
                head = row["parent_id"]
            return list(reversed(nodes))

    def _range(self, row, path):
        indexes = {m["id"]: i for i, m in enumerate(path)}
        if row["start_message_id"]:
            start = indexes.get(row["start_message_id"])
        elif row["before_message_id"]:
            before = indexes.get(row["before_message_id"])
            start = None if before is None else before + 1
        else:
            start = 0
        if start is None:
            return None
        end = indexes.get(row["end_message_id"]) if row["status"] == "closed" and row["end_message_id"] else (-1 if row["status"] == "closed" else len(path) - 1)
        if end is None or end < start - 1:
            return None
        return path[start:end + 1]

    def _visible(self, row, path):
        ids = {m["id"] for m in path}
        if row["status"] == "open":
            anchor = row["start_message_id"] or row["before_message_id"]
            return anchor is None or anchor in ids
        members = json.loads(row["members"])
        if not members:
            return (row["end_message_id"] is None and row["before_message_id"] is None) or row["end_message_id"] in ids or row["before_message_id"] in ids
        if all(m["id"] in ids for m in members):
            return True
        return bool(path) and (
            path[-1]["id"] in {m["id"] for m in members}
            or any(m["role"] == "assistant" and m["id"] in ids for m in members)
        )

    def _state(self, row, path):
        if json.loads(row["grants"]) != scope_snapshot(self.app, row["agent_id"]):
            return False, "permissions_changed"
        selected = self._range(row, path)
        if selected is None:
            return False, "branch_changed"
        if row["status"] == "closed" and _nodes(selected) != json.loads(row["members"]):
            return False, "sources_changed"
        if row["status"] == "closed" and not self._sources_valid(row):
            return False, "sources_unavailable"
        return True, None

    def _sources_valid(self, event):
        """Validate live references; missing assets never leave a usable summary."""
        with self.database.transaction() as db:
            for source in db.execute("SELECT * FROM work_event_sources WHERE event_id=?", (event["id"],)):
                ref = json.loads(source["ref"])
                if "record_id" in ref:
                    record = db.execute("SELECT * FROM work_event_records WHERE id=?", (ref["record_id"],)).fetchone()
                    if not record or record["sha256"] != source["sha256"] or record["agent_id"] != event["agent_id"] or record["conversation_id"] != event["conversation_id"]:
                        return False
                    actual = db.execute("SELECT coalesce(sum(length(body)),0) FROM work_event_chunks WHERE record_id=?", (ref["record_id"],)).fetchone()[0]
                    if actual != source["byte_count"]:
                        return False
                elif source["kind"] == "attachment":
                    asset = db.execute("SELECT blob_name,sha256,byte_count FROM attachments WHERE id=? AND agent_id=?", (ref["id"], event["agent_id"])).fetchone()
                    if asset is None:
                        return False
                    path = self.app.attachments.directory / asset["blob_name"]
                    if path.is_symlink() or path.resolve().parent != self.app.attachments.directory.resolve():
                        return False
                    try:
                        digest = hashlib.sha256()
                        with path.open("rb") as handle:
                            for part in iter(lambda: handle.read(65536), b""):
                                digest.update(part)
                        if digest.hexdigest() != asset["sha256"] or _hash(self._attachment_text(ref["id"])) != source["sha256"]:
                            return False
                    except (OSError, DomainError, ValueError):
                        return False
                elif source["kind"] in {"task", "task_step", "task_review", "task_event"}:
                    try:
                        if _hash(self._source_text(source)) != source["sha256"]:
                            return False
                    except DomainError:
                        return False
        return True

    def get(self, event_id, *, agent_id=None, conversation_id=None):
        row = self._row(event_id)
        if (agent_id is not None and row["agent_id"] != agent_id) or (
            conversation_id is not None and row["conversation_id"] != conversation_id
        ):
            raise NotFound("工作事件不在当前来源范围。")
        path = self._metadata_path(row["conversation_id"])
        if not self._visible(row, path):
            raise NotFound("工作事件不在当前分支。")
        valid, reason = self._state(row, path)
        with self.database.transaction() as db:
            count = db.execute("SELECT count(*) FROM work_event_sources WHERE event_id=?", (event_id,)).fetchone()[0]
            version = db.execute("SELECT kind FROM work_event_summaries WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)).fetchone()
            run_ids = json.loads(row["run_ids"])
            replay = []
            for run_id in run_ids:
                checkpoint = db.execute("SELECT json_extract(state,'$.phase') FROM chat_run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
                replay.append(checkpoint is not None and checkpoint[0] == "completed")
        return {
            "id": row["id"], "agent_id": row["agent_id"], "conversation_id": row["conversation_id"],
            "title": row["title"] if valid else "来源已失效的工作",
            "status": row["status"] if valid else "stale", "lifecycle": row["status"],
            "outcome": row["outcome"], "folded": bool(row["folded"]) and valid,
            "revision": row["revision"], "summary": row["summary"] if valid else "",
            "summary_kind": version[0] if version else "rule", "coverage": row["coverage"],
            "replay_coverage": "retained" if replay and all(replay) else "unavailable",
            "before_message_id": row["before_message_id"], "start_message_id": row["start_message_id"],
            "end_message_id": row["end_message_id"], "source_count": count,
            "message_ids": [m["id"] for m in json.loads(row["members"])],
            "valid": valid, "reason": reason, "created_at": row["created_at"], "closed_at": row["closed_at"],
        }

    def list(self, conversation_id):
        self.app.conversations.read(conversation_id)
        path = self._metadata_path(conversation_id)
        with self.database.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM work_events WHERE conversation_id=? ORDER BY created_at,id", (conversation_id,))]
        return [self.get(row["id"]) for row in rows if self._visible(row, path)]

    def begin(self, conversation_id, title, request_id, *, start_message_id=None):
        if not title.strip() or len(title) > 500 or not request_id or len(request_id) > 200:
            raise ValueError("工作标题或请求编号无效。")
        with self.database.transaction() as db:
            conversation = self.app.conversations.read(conversation_id)
            fingerprint = _hash(_json([conversation_id, title, start_message_id]))
            old = db.execute("SELECT id,fingerprint FROM work_events WHERE agent_id=? AND request_id=?", (conversation["agent_id"], request_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("工作请求编号已用于其他参数。")
                return self.get(old["id"])
            if any(e["lifecycle"] == "open" for e in self.list(conversation_id)):
                raise Conflict("当前分支已有未关闭的工作。")
            path = self.app.conversations.messages(conversation_id)
            before = conversation["head_id"]
            if start_message_id:
                index = next((i for i, m in enumerate(path) if m["id"] == start_message_id and m["role"] == "user"), None)
                if index is None:
                    raise NotFound("工作起点必须是当前分支上的用户消息。")
                before = path[index - 1]["id"] if index else None
            elif db.execute("SELECT 1 FROM chat_runs WHERE conversation_id=? AND status='running'", (conversation_id,)).fetchone():
                raise Conflict("请先结束当前回合，或明确选择工作起点。")
            event_id = new_id()
            db.execute(
                "INSERT INTO work_events(id,agent_id,conversation_id,before_message_id,start_message_id,end_message_id,title,status,outcome,folded,revision,request_id,fingerprint,grants,summary,coverage,members,run_ids,created_at,closed_at) "
                "VALUES(?,?,?,?,?,NULL,?,'open','pending',0,1,?,?,?,'','pending','[]','[]',?,NULL)",
                (event_id, conversation["agent_id"], conversation_id, before, start_message_id, title, request_id, fingerprint,
                 _json(scope_snapshot(self.app, conversation["agent_id"])), utc_now()),
            )
            db.execute("INSERT INTO work_event_audit VALUES(?,1,'begin',?)", (event_id, utc_now()))
            return self.get(event_id)

    def _revision(self, row, expected):
        if type(expected) is not int or row["revision"] != expected:
            raise Conflict("工作事件版本已改变，请刷新后重试。")

    def _runs(self, row, selected):
        members = {m["id"] for m in selected}
        users = {m["id"] for m in selected if m["role"] == "user"}
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT r.*,c.assistant_message_id FROM chat_runs r LEFT JOIN chat_run_checkpoints c ON c.run_id=r.id "
                "WHERE r.conversation_id=? ORDER BY r.created_at,r.id", (row["conversation_id"],)
            ).fetchall()
        return [dict(r) for r in rows if r["user_message_id"] in users and (
            not r["assistant_message_id"] or r["assistant_message_id"] in members
        )]

    def _safe(self, row, runs):
        with self.database.transaction() as db:
            for run in runs:
                if run["status"] not in {"completed", "failed", "cancelled"}:
                    raise Conflict("工作仍在进行、暂停或等待恢复，不能折叠。")
                if db.execute("SELECT 1 FROM chat_tool_effects WHERE run_id=? AND (status!='completed' OR json_extract(result,'$[1]')='unknown')", (run["id"],)).fetchone():
                    raise Conflict("工具副作用仍未知，不能关闭工作。")
                cp = db.execute("SELECT json_extract(state,'$.evidence.unknown_failures') FROM chat_run_checkpoints WHERE run_id=?", (run["id"],)).fetchone()
                if cp and cp[0] and json.loads(cp[0]):
                    raise Conflict("工作仍有未知执行结果，请先核对。")
                if any(s["status"] in {"running", "cancelling", "unknown"} for s in self.app.sandbox.run_sessions(row["agent_id"], run["id"], "chat")):
                    raise Conflict("命令尚未结束或清理未知。")
            links = db.execute("SELECT t.* FROM tasks t JOIN work_event_task_links l ON l.task_id=t.id WHERE l.event_id=?", (row["id"],)).fetchall()
            for task in links:
                if task["status"] not in {"completed", "needs_review", "cancelled"}:
                    raise Conflict("关联任务尚未结束。")
                if task["workspace"] and not db.execute("SELECT 1 FROM task_reviews WHERE task_id=? AND status='applied'", (task["id"],)).fetchone():
                    raise Conflict("关联任务的工作区交付尚未完成核对。")
                if db.execute("SELECT 1 FROM task_steps WHERE task_id=? AND status IN ('executing','unknown')", (task["id"],)).fetchone():
                    raise Conflict("关联任务的副作用仍未知。")
                if db.execute("SELECT 1 FROM task_reviews WHERE task_id=? AND status!='applied' AND json_extract(result,'$.replacement_review_id') IS NULL", (task["id"],)).fetchone():
                    raise Conflict("关联任务仍有待处理交付，不能自动折叠。")
                if any(s["status"] in {"running", "cancelling", "unknown"} for s in self.app.sandbox.run_sessions(row["agent_id"], task["id"], "task")):
                    raise Conflict("关联任务命令尚未安全结束。")
            return [dict(t) for t in links]

    def record(self, run_id, kind, text, *, step=0, call_id="", attempt=0, metadata=None):
        return self.records.record(run_id, kind, text, step=step, call_id=call_id, attempt=attempt, metadata=metadata)

    def run_closed(self, run_id):
        with self.database.transaction() as db:
            return db.execute("SELECT 1 FROM work_events e,json_each(e.run_ids) r WHERE e.status='closed' AND r.value=? LIMIT 1", (run_id,)).fetchone() is not None

    def _add_source(self, event_id, kind, ref, text, ordinal, *, coverage="retained", source_id=None):
        source_id = source_id or kind + ":" + _hash(_json(ref))[:32]
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO work_event_sources(event_id,source_id,kind,ref,byte_count,sha256,coverage,ordinal) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, source_id, kind, _json(ref), len(text.encode("utf-8")), _hash(text), coverage, ordinal),
            )
        return source_id

    def _capture_sources(self, row, selected, runs, tasks):
        event_id, ordinal = row["id"], 0
        for message in selected:
            self._add_source(event_id, "message", {"id": message["id"]}, message["text"], ordinal)
            ordinal += 1
            for attachment_id in message.get("attachment_ids", []):
                text = self._attachment_text(attachment_id)
                self._add_source(event_id, "attachment", {"id": attachment_id}, text, ordinal, coverage="bounded")
                ordinal += 1
        records = self.records.for_runs([r["id"] for r in runs])
        for record in records:
            captured_scope = {k: v for k, v in record["grants"].items() if k != "captured_from"}
            if captured_scope != json.loads(row["grants"]):
                raise Conflict("工作来源采集时的授权已改变，不能以新授权重标旧资料。")
            with self.database.transaction() as db:
                db.execute(
                    "INSERT INTO work_event_sources(event_id,source_id,kind,ref,byte_count,sha256,coverage,ordinal) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, "record:" + record["id"], record["kind"], _json({"record_id": record["id"]}),
                     record["byte_count"], record["sha256"], (record["metadata"] or {}).get("coverage", "retained"), ordinal),
                )
            ordinal += 1
        # Visible stream deltas are already safely persisted (unlike previews of
        # tools). Reference them, including partial output before a crash.
        with self.database.transaction() as db:
            for run in runs:
                stream = db.execute("SELECT sequence,event,json_extract(data,'$.text') AS text FROM chat_events WHERE run_id=? AND event IN ('text','thinking') ORDER BY sequence", (run["id"],))
                for entry in stream:
                    self._add_source(event_id, entry["event"], {"run_id": run["id"], "sequence": entry["sequence"]}, entry["text"] or "", ordinal)
                    ordinal += 1
        gap = False
        for run in runs:
            captured = [r for r in records if r["run_id"] == run["id"]]
            if not any(r["kind"] == "run_start" for r in captured):
                gap = True
            keys = {(r["kind"], r["step"], r["call_id"]) for r in captured}
            with self.database.transaction() as db:
                for effect in db.execute("SELECT step,call_id FROM chat_tool_effects WHERE run_id=?", (run["id"],)):
                    if ("tool_result", effect["step"], effect["call_id"]) not in keys:
                        gap = True
                for e in db.execute("SELECT event,data FROM chat_events WHERE run_id=? AND event IN ('tool_started','tool_result')", (run["id"],)):
                    data = json.loads(e["data"])
                    kind = "tool_call" if e["event"] == "tool_started" else "tool_result"
                    if (kind, data.get("step", 0), data.get("call_id", "")) not in keys:
                        gap = True
        with self.database.transaction() as db:
            for task in tasks:
                self._add_source(event_id, "task", {"id": task["id"]}, self._task_text(task["id"]), ordinal)
                ordinal += 1
                for step in db.execute("SELECT id FROM task_steps WHERE task_id=? ORDER BY model_step,call_id", (task["id"],)):
                    self._add_source(event_id, "task_step", {"task_id": task["id"], "step_id": step["id"]}, self._task_step_text(task["id"], step["id"]), ordinal)
                    ordinal += 1
                for review in db.execute("SELECT id FROM task_reviews WHERE task_id=? ORDER BY created_at,id", (task["id"],)):
                    self._add_source(event_id, "task_review", {"task_id": task["id"], "review_id": review["id"]}, self._task_review_text(task["id"], review["id"]), ordinal)
                    ordinal += 1
                for entry in db.execute("SELECT id,data FROM task_events WHERE task_id=? ORDER BY id", (task["id"],)):
                    self._add_source(event_id, "task_event", {"task_id": task["id"], "event_id": entry["id"]}, entry["data"], ordinal)
                    ordinal += 1
        return "capture_gap" if gap else "retained", ordinal

    def close(self, event_id, *, expected_revision):
        with self.database.transaction() as db:
            row = self._row(event_id)
            if row["status"] == "closed" and expected_revision in {row["revision"], row["revision"] - 1}:
                return self.get(event_id)
            self._revision(row, expected_revision)
            path = self.app.conversations.messages(row["conversation_id"])
            if not self._visible(row, path) or not self._state(row, path)[0]:
                raise Conflict("工作分支或来源授权已改变。")
            selected = self._range(row, path)
            runs = self._runs(row, selected)
            tasks = self._safe(row, runs)
            if not runs and not tasks:
                raise Conflict("尚无可关闭的工作运行。")
            selected_ids = {m["id"] for m in selected}
            for other in db.execute("SELECT members FROM work_events WHERE conversation_id=? AND status='closed' AND id!=?", (row["conversation_id"], event_id)):
                if selected_ids.intersection(m["id"] for m in json.loads(other[0])):
                    raise Conflict("工作区间与已有事件重叠。")
            states = {r["status"] for r in runs} | {t["status"] for t in tasks}
            outcome = "completed" if states == {"completed"} else "cancelled" if states == {"cancelled"} else "failed" if states == {"failed"} else "partial"
            coverage, count = self._capture_sources(row, selected, runs, tasks)
            verified = []
            for run in runs:
                last = db.execute("SELECT json_extract(data,'$.verification.status') FROM chat_events WHERE run_id=? AND event='complete' ORDER BY sequence DESC LIMIT 1", (run["id"],)).fetchone()
                verified.append(last[0] if last else "unverified")
            verified.extend("passed" if task["verified"] else "unverified" for task in tasks)
            verification = "已有验证通过记录" if verified and set(verified) == {"passed"} else "结果未全部核验"
            labels = {"completed": "执行已结束", "cancelled": "已取消", "failed": "执行失败", "partial": "流程结束，含未完成或失败结果"}
            summary = f"工作目标：{_clip(row['title'], 512)}；{labels[outcome]}，{verification}；保留 {count} 条来源。"
            revision = row["revision"] + 1
            end = path[-1]["id"] if path else row["before_message_id"]
            db.execute("UPDATE work_events SET status='closed',outcome=?,folded=?,revision=?,summary=?,coverage=?,members=?,run_ids=?,end_message_id=?,closed_at=? WHERE id=?", (outcome, int(coverage == "retained"), revision, summary, coverage, _json(_nodes(selected)), _json([r["id"] for r in runs]), end, utc_now(), event_id))
            db.execute("INSERT INTO work_event_summaries VALUES(?,?,'rule',?,?)", (event_id, revision, summary, utc_now()))
            db.execute("INSERT INTO work_event_audit VALUES(?,?,'close',?)", (event_id, revision, utc_now()))
            return self.get(event_id)

    def set_fold(self, event_id, folded, *, expected_revision):
        with self.database.transaction() as db:
            row = self._row(event_id)
            self._revision(row, expected_revision)
            value = self.get(event_id)
            if row["status"] != "closed" or (folded and (not value["valid"] or row["coverage"] != "retained")):
                raise Conflict("当前事件不满足安全折叠条件。")
            if bool(row["folded"]) == folded:
                return value
            db.execute("UPDATE work_events SET folded=?,revision=revision+1 WHERE id=?", (int(folded), event_id))
            db.execute("INSERT INTO work_event_audit VALUES(?,?,?,?)", (event_id, row["revision"] + 1, "fold" if folded else "raw", utc_now()))
            return self.get(event_id)

    def set_summary(self, event_id, text, *, expected_revision, kind="owner"):
        if not text.strip() or len(text.encode("utf-8")) > 2048 or kind not in {"owner", "model"}:
            raise ValueError("概况为空、超出 2 KiB 或生成类型无效。")
        with self.database.transaction() as db:
            row = self._row(event_id)
            self._revision(row, expected_revision)
            value = self.get(event_id)
            if row["status"] != "closed" or not value["valid"]:
                raise Conflict("只能更正当前有效的已关闭事件。")
            revision = row["revision"] + 1
            db.execute("UPDATE work_events SET summary=?,revision=? WHERE id=?", (text, revision, event_id))
            db.execute("INSERT INTO work_event_summaries VALUES(?,?,?,?,?)", (event_id, revision, kind, text, utc_now()))
            db.execute("INSERT INTO work_event_audit VALUES(?,?,'summary',?)", (event_id, revision, utc_now()))
            return self.get(event_id)

    def link_task(self, event_id, task_id, *, expected_revision, origin_message_id=None):
        with self.database.transaction() as db:
            row = self._row(event_id)
            self._revision(row, expected_revision)
            event = self.get(event_id)
            task = db.execute("SELECT * FROM tasks WHERE id=? AND agent_id=?", (task_id, row["agent_id"])).fetchone()
            if task is None:
                raise NotFound("任务不在当前个体范围。")
            checkpoint = db.execute("SELECT state FROM task_checkpoints WHERE task_id=?", (task_id,)).fetchone()
            captured = json.loads(checkpoint[0]).get("source_grants") if checkpoint else None
            if captured != json.loads(row["grants"]):
                raise Conflict("任务缺少相同的来源授权快照，不能将旧任务资料重新授予当前聊天。")
            if row["status"] != "open" or not event["valid"]:
                raise Conflict("只能向当前未关闭工作关联任务。")
            path = self.app.conversations.messages(row["conversation_id"])
            if origin_message_id and origin_message_id not in {m["id"] for m in path}:
                raise NotFound("发起锚点不在当前分支。")
            anchor = origin_message_id or (path[-1]["id"] if path else None)
            if db.execute("SELECT 1 FROM work_event_task_links WHERE event_id=? AND task_id=?", (event_id, task_id)).fetchone():
                return event
            db.execute("INSERT INTO work_event_task_links(event_id,task_id,agent_id,conversation_id,origin_message_id,return_message_id,grants,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id,task_id) DO NOTHING", (event_id, task_id, row["agent_id"], row["conversation_id"], anchor, anchor, row["grants"], utc_now()))
            db.execute("UPDATE work_events SET revision=revision+1 WHERE id=?", (event_id,))
            db.execute("INSERT INTO work_event_audit VALUES(?,?,'link_task',?)", (event_id, row["revision"] + 1, utc_now()))
            return self.get(event_id)

    def projection_items(self, conversation_id, messages):
        with self.database.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM work_events WHERE conversation_id=? AND status='closed' ORDER BY created_at,id", (conversation_id,))]
        values = []
        for row in rows:
            if not self._visible(row, messages):
                continue
            valid, reason = self._state(row, messages)
            values.append({"event_id": row["id"], "revision": row["revision"], "message_ids": [m["id"] for m in json.loads(row["members"])], "before_message_id": row["before_message_id"], "end_message_id": row["end_message_id"], "summary": f"状态：{row['outcome']}。" + row["summary"] if valid else "", "folded": bool(row["folded"]) and row["coverage"] == "retained", "valid": valid, "reason": reason, "coverage": row["coverage"]})
        return values

    def _readable(self, event_id, agent_id, conversation_id):
        value = self.get(event_id, agent_id=agent_id, conversation_id=conversation_id)
        if not value["valid"]:
            if value["reason"] == "permissions_changed":
                raise PermissionDenied("来源授权已改变，不能读取旧工作资料。")
            raise Conflict("工作来源或分支已失效。")
        if value["lifecycle"] != "closed":
            raise Conflict("工作尚未关闭，原始执行状态仍由运行器维护。")
        return value

    def _cursor(self, event, kind, offset, source_id=None):
        value = [event["id"], event["revision"], event["agent_id"], event["conversation_id"], kind, source_id, offset]
        return base64.urlsafe_b64encode(_json(value).encode()).decode().rstrip("=")

    def _offset(self, event, kind, cursor, source_id=None):
        if not cursor:
            return 0
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            expected = [event["id"], event["revision"], event["agent_id"], event["conversation_id"], kind, source_id]
            if not isinstance(value, list) or len(value) != 7 or value[:6] != expected or type(value[6]) is not int or value[6] < 0:
                raise ValueError
            return value[6]
        except (ValueError, TypeError, UnicodeError) as error:
            raise Conflict("读取游标已失效，请重新打开目录。") from error

    def directory(self, event_id, *, agent_id, conversation_id, cursor=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("目录页大小无效。")
        with self.database.transaction() as db:
            event = self._readable(event_id, agent_id, conversation_id)
            offset = self._offset(event, "directory", cursor)
            rows = db.execute("SELECT * FROM work_event_sources WHERE event_id=? ORDER BY ordinal LIMIT ? OFFSET ?", (event_id, limit + 1, offset)).fetchall()
            items = [{"id": r["source_id"], "source_id": r["source_id"], "kind": r["kind"], "location": r["source_id"], "coverage": r["coverage"], "byte_count": r["byte_count"], "sha256": r["sha256"]} for r in rows[:limit]]
            return {"event_id": event_id, "revision": event["revision"], "items": items, "next_cursor": self._cursor(event, "directory", offset + limit) if len(rows) > limit else None, "coverage": event["coverage"]}

    def _attachment_text(self, attachment_id):
        item = self.app.attachments.read(attachment_id)
        # Original blob remains in the content service; this is a bounded text
        # view, explicitly not a replacement for image/PDF/binary bytes.
        return _json({key: item.get(key) for key in ("id", "original_name", "sha256", "byte_count", "status", "warnings", "segments")})

    def _task_text(self, task_id):
        with self.database.transaction() as db:
            row = db.execute("SELECT id,objective,status,verified,final,error_code FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFound("任务来源已失效。")
            return _json(dict(row))

    def _task_step_text(self, task_id, step_id):
        with self.database.transaction() as db:
            row = db.execute("SELECT id,model_step,call_id,name,arguments,purpose,status,effect_id,result FROM task_steps WHERE task_id=? AND id=?", (task_id, step_id)).fetchone()
            if row is None:
                raise NotFound("任务步骤来源已失效。")
            return _json(dict(row))

    def _task_review_text(self, task_id, review_id):
        with self.database.transaction() as db:
            row = db.execute("SELECT id,manifest,status,result FROM task_reviews WHERE task_id=? AND id=?", (task_id, review_id)).fetchone()
            if row is None:
                raise NotFound("任务交付来源已失效。")
            return _json(dict(row))

    def _source_text(self, source):
        ref = json.loads(source["ref"])
        with self.database.transaction() as db:
            if source["kind"] == "message":
                row = db.execute("SELECT text FROM conversation_messages WHERE id=?", (ref["id"],)).fetchone()
                if row is None:
                    raise NotFound("原消息已删除。")
                return row[0]
            if source["kind"] == "attachment":
                return self._attachment_text(ref["id"])
            if source["kind"] == "task":
                return self._task_text(ref["id"])
            if source["kind"] == "task_step":
                return self._task_step_text(ref["task_id"], ref["step_id"])
            if source["kind"] == "task_review":
                return self._task_review_text(ref["task_id"], ref["review_id"])
            if source["kind"] == "task_event":
                row = db.execute("SELECT data FROM task_events WHERE task_id=? AND id=?", (ref["task_id"], ref["event_id"])).fetchone()
            else:
                row = db.execute("SELECT json_extract(data,'$.text') FROM chat_events WHERE run_id=? AND sequence=? AND event IN ('text','thinking')", (ref["run_id"], ref["sequence"])).fetchone()
            if row is None:
                raise NotFound("原始记录已失效。")
            return row[0] or ""

    def read_record(self, event_id, source_id, *, agent_id, conversation_id, cursor=None, max_bytes=8192):
        if type(max_bytes) is not int or not 4 <= max_bytes <= 16384:
            raise ValueError("正文页大小须在 4 到 16384 字节之间。")
        with self.database.transaction() as db:
            event = self._readable(event_id, agent_id, conversation_id)
            source = db.execute("SELECT * FROM work_event_sources WHERE event_id=? AND source_id=?", (event_id, source_id)).fetchone()
            if source is None:
                raise NotFound("原文不在当前事件目录。")
            offset = self._offset(event, "record", cursor, source_id)
            ref = json.loads(source["ref"])
            if "record_id" in ref:
                data = self.records.read(ref["record_id"], offset=offset, max_bytes=max_bytes)
                text, next_offset = data["text"], data.get("next_offset")
                if data["sha256"] != source["sha256"]:
                    raise Conflict("来源版本已改变。")
            elif source["kind"] == "message":
                row = db.execute("SELECT rowid FROM conversation_messages WHERE id=? AND conversation_id=?", (ref["id"], conversation_id)).fetchone()
                if row is None:
                    raise NotFound("原消息已删除。")
                with db.blobopen("conversation_messages", "text", row["rowid"], readonly=True) as body:
                    if offset > len(body):
                        raise Conflict("正文游标超过来源范围。")
                    body.seek(offset)
                    chunk = body.read(max_bytes)
                try:
                    text = chunk.decode("utf-8")
                except UnicodeDecodeError as error:
                    if error.end == len(chunk) and error.reason == "unexpected end of data":
                        text = chunk[:error.start].decode("utf-8")
                    else:
                        raise Conflict("正文游标不是有效文本边界。") from error
                next_offset = offset + len(text.encode("utf-8"))
                next_offset = next_offset if next_offset < source["byte_count"] else None
            else:
                raw = self._source_text(source)
                if _hash(raw) != source["sha256"]:
                    raise Conflict("原文已变更，请重新建立事件索引。")
                encoded = raw.encode("utf-8")
                if offset > len(encoded):
                    raise Conflict("正文游标超过来源范围。")
                try:
                    encoded[offset:].decode("utf-8")
                except UnicodeError as error:
                    raise Conflict("正文游标不是有效文本边界。") from error
                text = encoded[offset:offset + max_bytes].decode("utf-8", errors="ignore")
                next_offset = offset + len(text.encode("utf-8"))
                next_offset = next_offset if next_offset < len(encoded) else None
            return {"event_id": event_id, "revision": event["revision"], "source_id": source_id, "text": text, "next_cursor": self._cursor(event, "record", next_offset, source_id) if next_offset is not None else None, "location": source_id, "sha256": source["sha256"], "byte_count": source["byte_count"], "coverage": source["coverage"]}
