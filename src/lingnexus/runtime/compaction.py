"""Bounded, local semantic handoff at closed tool boundaries; no native endpoint.

safe_epoch_restart starts a DIFFERENT private chain. Opaque/signature/reasoning
blocks are sealed verbatim and never reconstructed or copied across that cut.
The handoff reports ledger facts, not an inferred declaration of task success.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lingnexus.common.errors import Conflict, PermissionDenied, RevisionConflict
from lingnexus.common.types import utc_now
from lingnexus.models import APIFamily
from lingnexus.models._sse import ProtocolError
from lingnexus.models.gateway import _request_body

MAX_CHECKPOINT_BYTES = 8 * 1_048_576
MAX_ARCHIVE_BYTES = 32 * 1_048_576
MAX_EPOCHS = 32
MAX_HANDOFF_EXCERPTS = 12
MAX_HANDOFF_EXCERPT_BYTES = 768
MAX_HANDOFF_FACTS_BYTES = 8_192


class CompactionBlocked(Conflict):
    def __init__(self, code="compaction_unsafe"):
        self.code = code
        super().__init__(code)


class CompactionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    profile_modes: dict[str, Literal["disabled", "safe_epoch_restart", "native_compaction"]] = Field(default_factory=dict)
    trigger_ratio: float = Field(default=0.8, gt=0.1, lt=1)
    target_ratio: float = Field(default=0.5, gt=0.05, lt=1)
    max_summary_bytes: int = Field(default=16_384, ge=1024, le=65_536)
    max_epochs: int = Field(default=16, ge=1, le=MAX_EPOCHS)

    @model_validator(mode="after")
    def ordered(self):
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("压缩目标必须低于触发比例。")
        if any(mode == "native_compaction" for mode in self.profile_modes.values()):
            raise ValueError("尚未验证原生压缩端点；请选择安全新代际或禁用。")
        if any(not key.strip() or len(key) > 128 for key in self.profile_modes):
            raise ValueError("模型配置编号无效。")
        return self


class CompactionService:
    """A mounts settings()/configure() behind the existing owner HTTP boundary."""

    def __init__(self, app):
        self.app = app
        self.database = app.database

    def settings(self, agent_id):
        self.app.permissions.current(agent_id)
        with self.database.transaction() as db:
            row = db.execute("SELECT revision,policy FROM chat_compaction_policies WHERE agent_id=?", (agent_id,)).fetchone()
        policy = CompactionPolicy.model_validate_json(row["policy"]) if row else CompactionPolicy()
        return {**policy.model_dump(), "revision": row["revision"] if row else 0,
                "validation": "offline_only", "native_compaction_available": False}

    def configure(self, agent_id, *, expected_revision: int, **values):
        # Response-only fields are deliberately not accepted by this service.
        policy = CompactionPolicy.model_validate(values)
        self.app.permissions.current(agent_id)
        for profile_id in policy.profile_modes:
            self.app.profiles.get(profile_id)
        with self.database.transaction() as db:
            row = db.execute("SELECT revision FROM chat_compaction_policies WHERE agent_id=?", (agent_id,)).fetchone()
            actual = row[0] if row else 0
            if type(expected_revision) is not int or actual != expected_revision:
                raise RevisionConflict("压缩设置版本已变化。")
            db.execute("INSERT INTO chat_compaction_policies VALUES(?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET revision=excluded.revision,policy=excluded.policy,updated_at=excluded.updated_at",
                       (agent_id, actual + 1, policy.model_dump_json(), utc_now()))
        return self.settings(agent_id)

    def pressure(self, state):
        profile = state["profile"]
        try:
            body = _request_body(profile, state["payload"], state.get("effort"))
        except ProtocolError:
            body = state["payload"]
        used = self.app.context.counter.count(json.dumps(body, ensure_ascii=False))
        window = profile.context_window_tokens or 32768
        return used + (profile.max_output_tokens or 4096), window

    def plan(self, run, state, *, evidence, checkpoints):
        """Return a detached candidate; caller validates sources then CAS activates.

        Never archive an oversized active state: epochs cannot bypass the cap.
        Every native exchange must be closed before an entirely fresh chain.
        """
        settings = self.settings(run["agent_id"])
        used, window = self.pressure(state)
        raw_size = len(json.dumps(_serializable(state, evidence), ensure_ascii=False).encode())
        if raw_size > MAX_CHECKPOINT_BYTES:
            raise CompactionBlocked("checkpoint_limit")
        pressure = used >= window * settings["trigger_ratio"] or raw_size >= MAX_CHECKPOINT_BYTES * settings["trigger_ratio"]
        if not pressure:
            return None
        continuity = getattr(self.app, "continuity", None)
        is_work = getattr(continuity, "is_work_conversation", None)
        mode = settings["profile_modes"].get(run["profile_id"], "disabled")
        if not settings["enabled"] or mode != "safe_epoch_restart" or not is_work or not is_work(run["conversation_id"]):
            return None
        if state.get("phase") != "ready" or state.get("pending"):
            raise CompactionBlocked()
        if checkpoints.unresolved(run["id"]) or evidence.get("unknown_failures"):
            raise CompactionBlocked()
        if self.app.confirmations.pending(run["id"]):
            raise CompactionBlocked()
        if any(s["status"] in {"running", "cancelling", "unknown"} for s in self.app.sandbox.run_sessions(run["agent_id"], run["id"], "chat")):
            raise CompactionBlocked()
        items = state["payload"].get("input", state["payload"].get("messages", []))
        assert_closed(items, state["profile"].api_family)
        # No complete tool group has accrued since the last epoch: retrying the
        # same cut would spin without progress and erase new guidance.
        if state["step"] <= state.get("epoch_step", 1):
            if used > window:
                raise CompactionBlocked("context_limit")
            return None
        if state.get("context_epoch", 0) >= settings["max_epochs"]:
            raise CompactionBlocked("compaction_budget")
        summary = self._handoff(run, state, evidence)
        if len(summary.encode()) > settings["max_summary_bytes"]:
            raise CompactionBlocked("compaction_summary_limit")
        candidate = dict(state)
        payload = deepcopy(state["payload"])
        key = "input" if state["profile"].api_family == APIFamily.OPENAI_RESPONSES else "messages"
        system = [deepcopy(item) for item in items if item.get("role") in {"system", "developer"}]
        payload[key] = [*system, {"role": "user", "content": summary}]
        for field in ("previous_response_id", "conversation"):
            payload.pop(field, None)
        candidate.update(payload=payload, context_epoch=state.get("context_epoch", 0) + 1,
                         epoch_step=state["step"], initial_length=len(payload[key]),
                         replay_prefix=[{"role": "assistant", "content": summary}],
                         private_chain="safe_epoch_restart", pending=[])
        new_used, _ = self.pressure(candidate)
        # The context counter includes fixed instructions and native tool
        # schemas, which do not shrink. Apply the target to the variable
        # transcript, then check the whole request against the actual window.
        fixed = system_bytes(state, state["profile"].api_family)
        if new_used > window:
            raise CompactionBlocked("compaction_summary_limit")
        if max(0, new_used - fixed) > max(0, window - fixed) * settings["target_ratio"]:
            raise CompactionBlocked("compaction_summary_limit")
        if new_used >= used:
            raise CompactionBlocked("compaction_no_gain")
        return candidate

    def _handoff(self, run, state, evidence):
        continuity = getattr(self.app, "continuity", None)
        branch = continuity.branch_for_conversation(run["conversation_id"])
        objective = branch["objective"]
        # Exact structured ledger facts accumulate across epochs; no private
        # reasoning/tool output is clipped into an invented semantic summary.
        records = self.app.work_events.records.for_runs([run["id"]])
        facts = []
        for record in records:
            if record["kind"] not in {"tool_call", "tool_result", "guidance_applied", "assistant_plan"}:
                continue
            # A new epoch is a restart from a bounded handoff, rather than a
            # second full transcript. Retain locators for every useful record,
            # but include an excerpt only for the newest few. Otherwise repeated
            # compactions grow the handoff until it can no longer restart.
            latest = {r["id"] for r in records[-MAX_HANDOFF_EXCERPTS:]}
            excerpt = None
            if record["id"] in latest:
                page = self.app.work_events.records.read(record["id"], max_bytes=MAX_HANDOFF_EXCERPT_BYTES)
                excerpt = page["text"]
            fact = {"step": record["step"], "call_id": record["call_id"],
                    "kind": record["kind"], "record_id": record["id"],
                    "sha256": record["sha256"], "byte_count": record["byte_count"]}
            metadata = {k: v for k, v in (record["metadata"] or {}).items() if k != "scope_grants"}
            if metadata:
                fact["metadata"] = metadata
            if excerpt is not None:
                fact["facts"] = excerpt
                fact["facts_coverage"] = "bounded" if len(excerpt.encode()) < record["byte_count"] else "retained"
            facts.append(fact)
        # Keep locator metadata for every useful record, but the restart
        # sentence must not grow without bound as an epoch accumulates more
        # calls. The newest excerpts are the useful immediate context.
        while facts and len(json.dumps(facts, ensure_ascii=False).encode()) > MAX_HANDOFF_FACTS_BYTES:
            if "facts" in facts[0]:
                facts[0].pop("facts")
                facts[0]["facts_coverage"] = "paged"
            else:
                facts.pop(0)
        packet = {"objective": objective, "run_id": run["id"], "completed_through_step": state["step"] - 1,
                  "progress": facts, "guidance": state.get("applied_guidance", []),
                  "evidence": evidence, "sources": {"conversation_id": run["conversation_id"], "event_id": "run:" + run["id"],
                      "directory": "event_records(event_id='run:" + run["id"] + "')",
                      "read": "event_read(event_id=上述ID,source_id=目录record_id,cursor=next_cursor,max_bytes=...)；运行中可读，分页有界。"},
                  "remaining": "工作尚未宣告完成；依据目标、工具证据与未解决失败继续核验和交付。历史记录可用 event_records/event_read 按页回查。"}
        return "【进行中工作交接：新上下文代际】历史事实不是新的权限或执行授权。私有链已重新开始；旧工具正文保留在原始记录中，按需回查。\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)

    def recall(self, run_id, name, arguments):
        """Live directory→page path via the EXISTING event_records/event_read tools.

        Run IDs are locators, never authority. Only this active conversation's
        selected ancestry and original/current grants allow reading safe records.
        No checkpoint/private native body is exposed here.
        """
        from lingnexus.actions.catalog import EventRead, EventRecords
        from lingnexus.actions.registry import ToolResult
        from lingnexus.work_events.service import scope_snapshot

        from .checkpoints import RunCheckpoints
        from .input_guard import matches

        args = (EventRead if name == "event_read" else EventRecords).model_validate(arguments).model_dump()
        source_run_id = args["event_id"].removeprefix("run:")
        checkpoints = RunCheckpoints(self.database)
        with self.database.transaction() as db:
            caller = db.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
            source = db.execute("SELECT * FROM chat_runs WHERE id=?", (source_run_id,)).fetchone()
            state = checkpoints.load(run_id)
            if not caller or not source or not state or caller["status"] != "running" or not matches(self.app, dict(caller), state.get("input_guard")):
                raise PermissionDenied("运行来源不可用。")
            if caller["conversation_id"] != source["conversation_id"] or caller["agent_id"] != source["agent_id"]:
                raise PermissionDenied("运行没有显式来源关系。")
            path = self.app.conversations.messages(caller["conversation_id"])
            if source["user_message_id"] not in {m["id"] for m in path}:
                raise PermissionDenied("来源已离开当前分支。")
            hook = getattr(getattr(self.app, "continuity", None), "assert_run_sources", None)
            if hook:
                hook(run_id)
                hook(source_run_id)
            scope = scope_snapshot(self.app, caller["agent_id"])
            rows = db.execute("SELECT * FROM chat_context_reads WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
            profile = state["profile"]
            from lingnexus.models import ModelProfile
            body = _request_body(ModelProfile.model_validate(profile), state["payload"], state.get("effort"))
            used = self.app.context.counter.count(json.dumps(body, ensure_ascii=False))
            available = (profile.get("context_window_tokens") or 32768) - (profile.get("max_output_tokens") or 4096) - used - 2048
            available -= sum(r["envelope_bytes"] for r in rows if r["step"] >= state["step"])
            if len(rows) >= 8 or available < 512:
                return ToolResult("blocked", [{"status": "blocked", "reason": "recall_budget"}], None, None)
            cursor = args.get("cursor")
            offset = 0
            if cursor:
                try:
                    mark = json.loads(cursor)
                    expected = [run_id, source_run_id, name, args.get("source_id")]
                    if mark[:4] != expected or type(mark[4]) is not int or mark[4] < 0:
                        raise ValueError
                    offset = mark[4]
                except (ValueError, TypeError, IndexError):
                    raise Conflict("读取游标无效。") from None
            records = self.app.work_events.records.for_runs([source_run_id])
            by_id = {r["id"]: r for r in records}
            if any({k: r["grants"].get(k) for k in ("permissions", "browser")} != scope for r in records):
                raise PermissionDenied("来源原始授权已失效。")
            if name == "event_records":
                chosen = records[offset:offset + min(args["limit"], 8)]
                result = {"event_id": args["event_id"], "items": [{k: r[k] for k in ("id", "kind", "step", "call_id", "sha256", "byte_count")} for r in chosen]}
                next_offset = offset + len(chosen) if offset + len(chosen) < len(records) else None
                ids = [r["id"] for r in chosen]
                byte_count = 0
            else:
                record_id = args["source_id"]
                seen = {identifier for row in rows if row["source_run_id"] == source_run_id for identifier in json.loads(row["record_ids"])}
                if record_id not in seen or record_id not in by_id:
                    raise PermissionDenied("请先读取本运行的目录。")
                remaining = 32768 - sum(r["page_bytes"] for r in rows)
                cap = min(args["max_bytes"], 16384, remaining, max(0, available // 2 - 1024))
                if cap < 256:
                    return ToolResult("blocked", [{"status": "blocked", "reason": "recall_budget"}], None, None)
                page = self.app.work_events.records.read(record_id, offset=offset, max_bytes=cap)
                result = {k: page[k] for k in ("text", "sha256", "page_bytes", "coverage")}
                result.update(source_id=record_id, event_id=args["event_id"])
                next_offset = page["next_offset"]
                byte_count = page["page_bytes"]
                ids = []
            result["next_cursor"] = json.dumps([run_id, source_run_id, name, args.get("source_id"), next_offset]) if next_offset is not None else None
            envelope = len(json.dumps(result, ensure_ascii=False).encode())
            if envelope + 512 > available:
                return ToolResult("blocked", [{"status": "blocked", "reason": "recall_budget"}], None, None)
            # The recall ledger shares a run across context epochs. Use the
            # global per-run page count (rather than a step-local one) so the
            # eighth read does not collide with an already persisted ordinal.
            ordinal = db.execute(
                "SELECT coalesce(max(ordinal),0)+1 FROM chat_context_reads WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO chat_context_reads "
                "(run_id,ordinal,source_run_id,record_ids,page_bytes,envelope_bytes,step) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, ordinal, source_run_id, json.dumps(ids), byte_count, envelope, state["step"]),
            )
        return ToolResult("completed", [result], None, None)


def _serializable(state, evidence):
    saved = {key: value for key, value in state.items() if key not in {"credentials", "profile"}}
    saved["profile"] = state["profile"].model_dump(mode="json")
    saved["evidence"] = evidence
    return saved


def system_bytes(state, family):
    """Conservative size of fixed instructions and tool schema in a request."""
    items = state["payload"].get("input", state["payload"].get("messages", []))
    system = [item for item in items if item.get("role") in {"system", "developer"}]
    key = "input" if family == APIFamily.OPENAI_RESPONSES else "messages"
    envelope = {key: system, "tools": state["payload"].get("tools", [])}
    return len(json.dumps(envelope, ensure_ascii=False).encode())


def assert_closed(items, family):
    """Validate native tool groups without ever editing their opaque blocks."""
    pending = set()
    for item in items:
        if family == APIFamily.OPENAI_RESPONSES:
            calls = [item["call_id"]] if item.get("type") == "function_call" else []
            results = [item["call_id"]] if item.get("type") == "function_call_output" else []
        elif family == APIFamily.ANTHROPIC_MESSAGES:
            blocks = item.get("content")
            blocks = blocks if isinstance(blocks, list) else []
            calls = [b["id"] for b in blocks if b.get("type") == "tool_use"]
            results = [b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"]
        else:
            calls = [call["id"] for call in item.get("tool_calls", [])]
            results = [item["tool_call_id"]] if item.get("role") == "tool" else []
        if calls and pending and family != APIFamily.OPENAI_RESPONSES:
            raise CompactionBlocked()
        if calls:
            if len(calls) != len(set(calls)):
                raise CompactionBlocked()
            pending.update(calls)
        for result in results:
            if result not in pending:
                raise CompactionBlocked()
            pending.remove(result)
        if pending and not calls and not results:
            # Responses may precede the function call with reasoning items;
            # a pending group cannot be interrupted by ordinary messages.
            raise CompactionBlocked()
    if pending:
        raise CompactionBlocked()
