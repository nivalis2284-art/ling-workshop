"""Chat orchestration without transport/API business logic or provider SDKs."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from lingnexus.actions.dispatch import ToolDispatcher
from lingnexus.common.errors import Conflict, DomainError
from lingnexus.common.execution_leases import ExecutionLeases, result_fence
from lingnexus.common.types import new_id, utc_now
from lingnexus.common.verification import account, ensure_current, initial, public
from lingnexus.configuration.settings import read_int
from lingnexus.models import (
    APIFamily,
    ModelEvent,
    ModelGateway,
    ModelProfile,
    RuntimeCredentials,
    ToolCall,
)
from lingnexus.models._sse import ProtocolError
from lingnexus.models.gateway import _request_body

from . import attachments, input_guard
from .checkpoints import CheckpointLimit, RunCheckpoints
from .compaction import CompactionBlocked, CompactionService
from .failover import ChatFailover, FailoverBlocked
from .hooks import HookEvent
from .payload import (
    append_native_response,
    append_tool_calls,
    append_tool_results,
    append_user_message,
    build_payload,
    declare_tools,
)
from .scope import ChatToolScope
from .secret_guard import CredentialEcho, SecretGuard
from .store import MAX_EVENT_CURSOR, RunStore
from .types import ChatRequest

if TYPE_CHECKING:
    from lingnexus.app import Application


ERROR_MESSAGES = {
    "invalid_request": "The request is invalid for this model profile.",
    "http_error": "The model endpoint rejected the request.",
    "content_type": "The model endpoint did not return an event stream.",
    "transport_error": "The model connection failed.",
    "timeout": "The model stream exceeded its time limit.",
    "malformed_stream": "The model stream contained invalid data.",
    "unexpected_eof": "The model stream ended before completion.",
    "stream_limit": "The model stream exceeded its limit.",
    "provider_error": "The provider reported a model error.",
    "incomplete_response": "The provider could not complete the response.",
    "credentials_unavailable": "The backup model credentials are unavailable for this run.",
    "profile_unavailable": "The backup model profile is unavailable for this run.",
    "context_unavailable": "The backup cannot fit the required chat context.",
    "context_invalidated": "The chat context changed while the response was generating.",
    "invalid_tool_arguments": "The provider returned an invalid tool request.",
    "step_budget_exhausted": "The run reached its step limit before finishing.",
    "credential_in_response": "The service returned sensitive runtime material; this response was discarded.",
    "runtime_error": "The chat run could not be completed.",
    "cancelled": "The owner cancelled this run.",
    "interrupted": "The server stopped before this run completed.",
    "context_limit": "当前运行的完整工具交换已超出模型窗口，请拆分任务；可以关闭已结束的工作后折叠其历史。",
    "checkpoint_limit": "当前运行超出原生恢复点大小上限，不能自动续跑；已安全保留的经历可在核对结果后关闭。",
    "command_session_active": "命令在模型准备结束时仍处于活动状态；已请求停止，请核对命令结果后新建请求。",
    "command_session_unknown": "命令清理状态未知；请人工核对命令与工作目录后再继续。",
    "command_cleanup_unknown": "取消后命令清理未能确认；请人工核对命令与工作目录。",
    "compaction_unsafe": "上下文压缩被未闭合工具或未知副作用阻止，已保留恢复状态。",
    "compaction_budget": "本工作已达到上下文代际上限，请核对后继续。",
    "compaction_summary_limit": "必要的工作交接超出安全预算，已停止而未丢弃恢复点。",
    "compaction_no_gain": "必要的交接无法减少上下文，已安全停止。",
}

COMPLETE_REASONS = {
    APIFamily.OPENAI_CHAT_COMPLETIONS: ("stop",),
    APIFamily.ANTHROPIC_MESSAGES: ("end_turn", "stop_sequence"),
    APIFamily.OPENAI_RESPONSES: ("completed",),
}

DEFAULT_MAX_STEPS = 40
MAX_STEPS_SETTING = "chat.max_steps"
_PREVIEW_CHARS = 800
_READ_ONLY_TOOLS = frozenset({"read_file", "search"})
"""Calls safe to overlap: they only read granted locations and hold no state."""


@dataclass
class _Turn:
    """One model call's collected result within a step."""

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    error: ModelEvent | None = None
    eof: bool = False
    continuation: dict | None = None


@dataclass
class _RunControl:
    """Owner intervention for one active run: pause, resume and mid-run guidance."""

    pause_requested: bool = False
    guides: list[str] = field(default_factory=list)
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)


def _tool_text(result) -> str:
    """The model-facing text for one tool result; structured payloads become JSON."""
    content = result.content
    if isinstance(content, str):
        return content
    if isinstance(content, list) and len(content) == 1:
        return json.dumps(content[0], ensure_ascii=False)
    if content is None:
        return json.dumps({"status": result.status}, ensure_ascii=False)
    return json.dumps(content, ensure_ascii=False)


def _preview(text: str) -> str:
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


def _call_batches(calls: list[ToolCall]) -> list[list[tuple[ToolCall, int]]]:
    """Group consecutive read-only calls so the batch can run concurrently.

    Side-effecting calls stay singleton batches: their order against reads,
    owner confirmations and file effects must never change under concurrency.
    """
    batches: list[list[tuple[ToolCall, int]]] = []
    concurrent: list[tuple[ToolCall, int]] = []
    for position, call in enumerate(calls):
        if call.name in _READ_ONLY_TOOLS:
            concurrent.append((call, position))
            continue
        if concurrent:
            batches.append(concurrent)
            concurrent = []
        batches.append([(call, position)])
    if concurrent:
        batches.append(concurrent)
    return batches


class ChatRuntime:
    """One manager per database/process, used on the server's asyncio loop.

    submit(ChatRequest) atomically saves a user message and a run, then schedules
    the model independently of subscribers. It returns a run dict with id, status,
    profile_id, conversation_id, user_message_id and context_manifest. A repeated
    (agent_id, request_id) with identical input returns that run, including
    terminal runs, without another model call or requiring credentials. Different
    input or an overlapping turn for that agent raises Conflict (409).

    One turn is a bounded loop: each step calls the model with the accumulated
    messages, collects every tool_call, runs each one through the BEFORE_TOOL /
    dispatch / AFTER_TOOL sequence, appends the native results, and repeats until
    the model stops asking for tools or the step budget runs out. Adjacent
    read-only calls (read_file, search) run concurrently; every other call keeps
    its order, and events stay in request order. Tools are only declared when the
    profile supports them; a run that exhausts its budget is reported as
    unfinished, never as completed. All policy lives in hooks.

    events(run_id, after=0) yields durable {id, event, data} records. id is a
    positive per-run sequence, and after is exclusive. started carries
    {run_id, conversation_id}; step_started carries {run_id, step}; tool_started
    and tool_result carry {run_id, step, name, call_id, ...}; a tool_result
    released without confirmation by full-access mode also carries
    access: "full"; paused/resumed carry
    {run_id}; text/thinking carry {run_id, text}; usage carries
    {run_id, step, attempt, profile_id, snapshot: true, usage}; complete
    carries {run_id, status: completed, assistant_message_id}; error carries
    {run_id, code, message}. Only complete/error end a subscriber. Thinking is
    provider-returned visible display material, never an assistant message.
    notice carries a safe code/message (model_switch, context_truncated,
    effort_omitted, step_budget_exhausted); model_switch carries
    {run_id, from_profile, to_profile, restart: true}. Subscribers must reset
    both visible buffers at that marker. Earlier partial events stay replayable.

    pause/resume/guide act at step boundaries and never kill an in-flight tool.
    A guide is injected as a user turn before the next step. Tool activity is kept
    as replayable run events; only user and final assistant text is persisted to
    the conversation.

    cancel(run_id) is idempotent and terminal. Closing/cancelling events() only
    detaches its subscriber. aclose() interrupts active runs and closes owned
    clients; injected clients remain caller-owned. Construction recovers rows
    left running after a process crash as interrupted. No crash reissue is
    attempted. Unavailable main calls can restart once on the designated backup;
    that conversation retains its backup binding across subsequent turns.

    Only the selected conversation branch supplies transcript messages. ID-less
    clients use a persistent default conversation. The request opening is the
    owner's system instructions, the individual's name, and a recent transcript
    tail; over-budget context is truncated and reported, never failed. Completion
    revalidates only the selected head. Attachments are linked to the user message
    with immutable digests. Replay uses durable input and never rereads files.
    """

    def __init__(self, application: "Application", *, client: httpx.AsyncClient | None = None):
        self.app = application
        self.store = RunStore(application.database)
        self.failover = ChatFailover(application, self.store)
        self.dispatcher = ToolDispatcher(mcp=getattr(application, "mcp", None))
        self._client = client
        self._owns_client = client is None
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_requested: set[str] = set()
        self._stop_operations: dict[str, asyncio.Task] = {}
        self._control: dict[str, _RunControl] = {}
        self._evidence: dict[str, dict] = {}
        self.checkpoints = RunCheckpoints(application.database)
        self.compaction = CompactionService(application)
        self.execution_leases = ExecutionLeases(application)
        self.execution_leases.recover_orphans()
        self._states: dict[str, dict] = {}
        self._closed = False
        with self.app.database.transaction() as db:
            pending = db.execute("SELECT id FROM chat_runs WHERE status='running'").fetchall()
            for row in pending:
                self._fail(row["id"], "interrupted", status="interrupted")

    def get_run(self, run_id: str) -> dict:
        """Return durable status and source references, without model/credential internals."""
        with self.app.database.transaction() as db:
            checkpoint = self.checkpoints.load(run_id) or {}
            run = self.store.get(run_id)
            error_row = db.execute(
                "SELECT data FROM chat_events WHERE run_id=? AND event='error' ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            terminal_code = json.loads(error_row[0]).get("code") if error_row else None
            version_unknown = bool(checkpoint.get("grants", {}).get("writable")) and checkpoint.get("workspace_version") is None
            source_valid = input_guard.matches(self.app, run, checkpoint.get("input_guard"))
            recoverable = (run["status"] in {"interrupted", "failed"}
                           and bool(checkpoint) and not self.checkpoints.uncertain(run_id)
                           and not checkpoint.get("evidence", {}).get("unknown_failures")
                           and checkpoint.get("phase") != "completed"
                           and terminal_code not in {"context_limit", "checkpoint_limit"} and not version_unknown
                           and source_valid and not self.app.work_events.run_closed(run_id))
            reason = (
                ERROR_MESSAGES["checkpoint_limit"] if terminal_code == "checkpoint_limit" else
                "上下文超出模型窗口，无法从相同输入续跑；请拆分任务或新建请求。"
                if terminal_code == "context_limit"
                else "原对话、来源授权或恢复点已失效，请新建请求。"
                if checkpoint and run["status"] in {"interrupted", "failed"} and not source_valid
                else "无法确认工作目录版本，请人工核对后新建请求。" if version_unknown
                else None if recoverable else "运行已结束或存在待核对的副作用。"
            )
            return self.store.get(run_id) | self.failover.metadata(run_id) | {
                "verification": public(self._evidence.get(run_id, checkpoint.get("evidence", {}))),
                "recoverable": recoverable,
                "recovery_reason": reason,
            }

    def submit(self, value: ChatRequest) -> dict:
        """Validate before network activity; rejected new requests have no persisted effects."""
        if self._closed:
            raise Conflict("Chat runtime is shutting down.")
        loop = asyncio.get_running_loop()
        # Null/omitted IDs keep legacy fingerprints; explicit IDs distinguish conversations.
        excluded = {"conversation_id"} if value.conversation_id is None else set()
        if value.regenerate_message_id is None:
            excluded.add("regenerate_message_id")
        fingerprint = hashlib.sha256(value.model_dump_json(exclude=excluded).encode()).hexdigest()
        with self.app.database.transaction() as db:
            if value.regenerate_message_id is not None and value.conversation_id is None:
                raise DomainError("Regeneration requires an explicit conversation_id.")
            old = db.execute(
                "SELECT id,fingerprint FROM chat_runs WHERE agent_id=? AND request_id=?",
                (value.agent_id, value.request_id),
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("The request_id already belongs to different chat input.")
                return self.get_run(old["id"])
            run_id = new_id()
            conversation = (
                self.app.conversations.for_chat(value.agent_id, value.conversation_id)
                if value.conversation_id is not None
                else self._default_conversation(value.agent_id)
            )
            self._admit_conversation(value.agent_id, conversation["id"])
            selected = self.failover.select(value.profile_id, conversation["id"])
            profile_id, profile = selected.profile_id, selected.profile
            # Manual/primary inputs retain preflight validation. A backup uses
            # its default effort when it lacks the owner's requested capability.
            if (
                value.effort is not None
                and value.effort not in profile.capabilities.effort_levels
                and selected.reason in ("primary", "manual")
            ):
                raise DomainError("The selected model does not support this effort level.")
            if value.regenerate_message_id is not None:
                nodes = self.app.conversations.messages(conversation["id"])
                user = next((n for n in nodes if n["id"] == value.regenerate_message_id), None)
                if user is None or user["role"] != "user" or user["text"] != value.message:
                    raise DomainError(
                        "Regeneration must match a user message on the selected branch."
                    )
                ids = attachments.regeneration_ids(self.app, user["id"], value.attachment_ids)
                attachments.preflight(self.app, value.agent_id, profile, ids)
                self.app.conversations.select_head(conversation["id"], user["id"])
            else:
                plans = attachments.preflight(
                    self.app, value.agent_id, profile, value.attachment_ids
                )
                user = self.app.conversations.append_user(
                    conversation["id"], value.message, request_id=f"chat:{run_id}:user"
                )
                attachments.link(self.app, conversation["id"], user["id"], plans)
            user_id = user["id"]
            messages = self.app.conversations.messages(conversation["id"])
            payload, manifest = build_payload(
                self.app, value.agent_id, profile, messages
            )
            db.execute(
                "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
                "conversation_id,user_message_id,context_manifest,created_at) "
                "VALUES (?,?,?,?,?,'running',?,?,?,?)",
                (
                    run_id,
                    value.agent_id,
                    value.request_id,
                    fingerprint,
                    profile_id,
                    conversation["id"],
                    user_id,
                    json.dumps(manifest),
                    utc_now(),
                ),
            )
            self.store.append(
                run_id, "started", {"run_id": run_id, "conversation_id": conversation["id"]}
            )
            from lingnexus.work_events.service import scope_snapshot

            self.app.work_events.record(run_id, "run_start", "运行开始", metadata={
                "capture_version": 1, "scope_grants": scope_snapshot(self.app, value.agent_id),
            })
            if manifest["truncated_messages"]:
                self.store.append(
                    run_id,
                    "notice",
                    {
                        "run_id": run_id,
                        "code": "context_truncated",
                        "truncated_messages": manifest["truncated_messages"],
                        "message": (
                            "对话超出模型窗口，已丢弃最早的 "
                            f"{manifest['truncated_messages']} 条消息。"
                        ),
                    },
                )
            self.failover.save_selection(run_id, selected)
            capture_dependencies = getattr(getattr(self.app, "continuity", None), "capture_run_dependencies", None)
            if capture_dependencies:
                capture_dependencies(run_id, manifest.get("selected_message_ids", []))
            effort = self.failover.effort(run_id, profile_id, profile, value.effort)
            admission = {"input_guard": input_guard.capture(self.app, self.store.get(run_id)),
                         "source_grants": scope_snapshot(self.app, value.agent_id),
                         "event_projection": manifest.get("work_events", [])}
        self._control[run_id] = _RunControl()
        self._evidence[run_id] = initial(coding=True)
        task = loop.create_task(
            self._execute(run_id, profile, selected.credentials, payload, effort, value.effort, admission=admission)
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._finish_task(run_id, done))
        return self.get_run(run_id)

    async def events(self, run_id: str, *, after: int = 0) -> AsyncGenerator[dict, None]:
        """Replay and tail committed events; subscriber cancellation never touches the worker."""
        if not 0 <= after <= MAX_EVENT_CURSOR:
            raise DomainError(f"The event cursor must be between 0 and {MAX_EVENT_CURSOR}.")
        while True:
            events, terminal = self.store.read_events(run_id, after)
            for event in events:
                after = event["id"]
                yield event
            if not events and terminal:
                return
            if not events:
                await asyncio.sleep(0.05)

    async def cancel(self, run_id: str) -> dict:
        """Cancel the model worker and wait boundedly for its owned commands."""
        self.get_run(run_id)
        operation = self._stop_operations.get(run_id)
        if operation is None:
            operation = asyncio.create_task(self._cancel_and_drain(run_id))
            self._stop_operations[run_id] = operation
            operation.add_done_callback(
                lambda done: self._stop_operations.pop(run_id, None)
                if self._stop_operations.get(run_id) is done else None
            )
        await asyncio.shield(operation)
        return self.get_run(run_id)

    async def _cancel_and_drain(self, run_id: str) -> None:
        was_running = self.get_run(run_id)["status"] == "running"
        # A model error can end the worker after a tool returned `running`.
        # The owner can still cancel that orphaned command session.
        self._stop_requested.add(run_id)
        if (task := self._tasks.get(run_id)) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        run = self.store.get(run_id)
        sessions = await self.app.sandbox.stop_run_sessions(
            run["agent_id"], run_id, "chat", timeout=5.0
        )
        unresolved = any(
            item["status"] in {"running", "cancelling", "unknown"} for item in sessions
        )
        if unresolved:
            self._mark_command_uncertain(run_id, "owner_cancel_cleanup_unconfirmed")
            if was_running:
                self._fail(run_id, "command_cleanup_unknown", status="failed")
        elif was_running:
            self._fail(run_id, "cancelled", status="cancelled")
        self._stop_requested.discard(run_id)

    def _mark_command_uncertain(self, run_id: str, reason: str) -> None:
        checkpoint = self.checkpoints.load(run_id)
        if not checkpoint:
            return
        evidence = checkpoint.setdefault("evidence", initial(coding=True))
        failures = evidence.setdefault("unknown_failures", {})
        failures["command_lifecycle"] = reason
        evidence["verified"] = False
        evidence.setdefault("failures", {})["command_lifecycle"] = reason
        checkpoint["phase"] = "command_lifecycle_unresolved"
        self.checkpoints.save(run_id, checkpoint)

    def pause(self, run_id: str) -> dict:
        """Request a pause at the next step boundary; a running tool is never killed."""
        self.get_run(run_id)
        control = self._control.get(run_id)
        if control is None:
            raise Conflict("该对话回合不在进行中，无法暂停。")
        control.pause_requested = True
        return {"run_id": run_id, "paused": True}

    def resume(self, run_id: str) -> dict:
        """Resume a paused run; the loop continues from the same step boundary."""
        self.get_run(run_id)
        control = self._control.get(run_id)
        if control is None:
            raise Conflict("该对话回合不在进行中，无法恢复。")
        control.pause_requested = False
        control.resume_event.set()
        return {"run_id": run_id, "paused": False}

    def recover(self, run_id: str, *, additional_steps: int = 0) -> dict:
        if self._closed or type(additional_steps) is not int or not 0 <= additional_steps <= 1000:
            raise Conflict("无法恢复或步数扩额无效。")
        run = self.get_run(run_id)
        sessions = self.app.sandbox.run_sessions(run["agent_id"], run_id, "chat")
        if any(item["status"] in {"running", "cancelling", "unknown"} for item in sessions):
            raise Conflict("命令仍活动或清理状态未知；请人工核对后新建请求。")
        if run["status"] == "running":
            return run
        if not run["recoverable"]:
            if run.get("recovery_reason") and "上下文超出模型窗口" in run["recovery_reason"]:
                raise Conflict(run["recovery_reason"])
            raise Conflict("此运行缺少安全恢复点，或副作用仍待人工核对。")
        checkpoint = self.checkpoints.load(run_id)
        profile, credentials = self.app.profiles.resolve(run["profile_id"])
        if profile.model_dump(mode="json") != checkpoint["profile"]:
            raise Conflict("模型配置已改变，请核对后新建请求。")
        self.failover.validate_head(run)
        scope = ChatToolScope(self.app, run["agent_id"], run_id)
        from lingnexus.common.verification import workspace_version

        actual_version = workspace_version(scope.workdir)
        if (scope.workdir is not None and actual_version is None) or checkpoint.get("workspace_version") != actual_version:
            raise Conflict("工作目录内容已变化，不能自动续跑旧状态。")
        if checkpoint.get("grants") != self.app.permissions.current(run["agent_id"]):
            raise Conflict("目录授权已改变，请核对后新建请求。")
        with self.app.database.transaction() as db:
            # Re-read under the transaction: deletion/relinking after get_run
            # must not turn a stale checkpoint into a new model request.
            current = self.store.get(run_id)
            if not input_guard.matches(self.app, current, checkpoint.get("input_guard")):
                raise Conflict("原对话或来源授权已失效，请新建请求。")
            self._admit_conversation(run["agent_id"], run["conversation_id"])
            db.execute("UPDATE chat_runs SET status='running',finished_at=NULL WHERE id=?", (run_id,))
        checkpoint["step_limit"] = checkpoint.get("step_limit", self._max_steps()) + additional_steps
        self._control[run_id] = _RunControl()
        self._evidence[run_id] = checkpoint.get("evidence", initial(coding=True))
        self.store.append(run_id, "resumed", {"run_id": run_id})
        task = asyncio.get_running_loop().create_task(self._execute(
            run_id, profile, credentials, checkpoint["payload"], checkpoint.get("effort"),
            checkpoint.get("requested_effort"), recovery=checkpoint))
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._finish_task(run_id, done))
        return self.get_run(run_id)

    def _save_checkpoint(self, run_id: str, state: dict) -> None:
        from lingnexus.common.verification import workspace_version

        run = self.store.get(run_id)
        saved = {key: value for key, value in state.items() if key not in {"credentials", "profile"}}
        saved["profile"] = state["profile"].model_dump(mode="json")
        saved["evidence"] = self._evidence.get(run_id, initial(coding=True))
        saved["workspace_version"] = workspace_version(ChatToolScope(self.app, run["agent_id"], run_id).workdir)
        saved["grants"] = state.get("source_grants", {}).get("permissions", self.app.permissions.current(run["agent_id"]))
        self.checkpoints.save(run_id, saved, secret=state["credentials"].api_key)

    def _default_conversation(self, agent_id):
        main = getattr(getattr(self.app, "continuity", None), "main", None)
        if main is not None and main.binding(agent_id) is not None:
            return main.conversation(agent_id)
        return self.app.conversations.default_real(agent_id)

    def _admit_conversation(self, agent_id, conversation_id):
        """One main/legacy lane + one explicitly related work lane per agent."""
        checker = getattr(getattr(self.app, "continuity", None), "is_work_conversation", None)
        work = bool(checker and checker(conversation_id))
        with self.app.database.transaction() as db:
            rows = db.execute("SELECT conversation_id FROM chat_runs WHERE agent_id=? AND status='running'", (agent_id,)).fetchall()
            for row in rows:
                if row[0] == conversation_id or bool(checker and checker(row[0])) == work:
                    raise Conflict("这个体的同类上下文已有运行；允许主干和一个工作分支并行。")

    def _ready_checkpoint(self, run_id, state):
        """Pressure planning precedes both ready save and model_inflight save."""
        self._assert_input_current(run_id)
        run = self.store.get(run_id)
        evidence = self._evidence.get(run_id, initial(coding=True))
        candidate = self.compaction.plan(run, state, evidence=evidence, checkpoints=self.checkpoints)
        if candidate is None:
            self._save_checkpoint(run_id, state)
            return
        from lingnexus.common.verification import workspace_version

        from .compaction import _serializable

        expected = self.checkpoints.encoded(run_id)
        # First run has no completed group; no valid candidate can exist yet.
        self._assert_input_current(run_id)
        def snapshot(value):
            saved = _serializable(value, evidence)
            saved["workspace_version"] = workspace_version(ChatToolScope(self.app, run["agent_id"], run_id).workdir)
            saved["grants"] = value.get("source_grants", {}).get("permissions", self.app.permissions.current(run["agent_id"]))
            return saved
        with self.app.database.transaction():
            self._assert_input_current(run_id)
            self.checkpoints.activate_epoch(run_id, snapshot(candidate), expected_state=expected,
                                            sealed_state=snapshot(state), secret=state["credentials"].api_key)
        state.update(candidate)
        self.store.append(run_id, "notice", {"run_id": run_id, "code": "context_epoch_started", "epoch": state["context_epoch"],
                                           "message": "工作已在安全工具边界进入新的上下文代际。"})

    def _assert_input_current(self, run_id: str) -> None:
        state = self._states.get(run_id, {})
        if not input_guard.matches(self.app, self.store.get(run_id), state.get("input_guard")):
            raise FailoverBlocked("context_invalidated")
        source_check = getattr(getattr(self.app, "continuity", None), "assert_run_sources", None)
        if source_check:
            try:
                source_check(run_id)
            except DomainError as error:
                raise FailoverBlocked("context_invalidated") from error
        for projection in state.get("event_projection", []):
            if not projection.get("sent"):
                continue
            dependencies = ([projection] if projection.get("event_id") else projection.get("sources", []))
            for dependency in dependencies:
                try:
                    event = self.app.work_events.get(dependency["event_id"])
                except DomainError as error:
                    raise FailoverBlocked("context_invalidated") from error
                if projection["strategy"] != "unavailable" and (event["revision"] != dependency["revision"] or not event["valid"]):
                    raise FailoverBlocked("context_invalidated")
        # A current run may have read an event whose overview did not fit its
        # initial window. Those freshly returned sources need the same fence.
        with self.app.database.transaction() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='work_event_reads' AND type='table'").fetchone()
            reads = db.execute("SELECT DISTINCT event_id,event_revision FROM work_event_reads WHERE run_id=?", (run_id,)).fetchall() if exists else []
        for reference in reads:
            try:
                event = self.app.work_events.get(reference["event_id"])
            except DomainError as error:
                raise FailoverBlocked("context_invalidated") from error
            if not event["valid"] or event["revision"] != reference["event_revision"]:
                raise FailoverBlocked("context_invalidated")

    def guide(self, run_id: str, text: str) -> dict:
        """Queue a mid-run instruction, injected as a user turn at the next boundary."""
        run = self.get_run(run_id)
        control = self._control.get(run_id)
        if control is None:
            raise Conflict("该对话回合不在进行中，无法提交指导。")
        state = self._states.get(run_id)
        credentials = state["credentials"] if state else self.app.profiles.resolve(run["profile_id"])[1]
        if credentials.api_key in text:
            raise Conflict("指导包含当前模型凭据，无法保存。")
        from lingnexus.work_events.service import scope_snapshot

        self.app.work_events.record(run_id, "guidance", text, call_id=new_id(), metadata={
            "delivery": "queued", "scope_grants": state.get("source_grants") if state else scope_snapshot(self.app, run["agent_id"]),
        })
        control.guides.append(text)
        return {"run_id": run_id, "queued": len(control.guides)}

    async def aclose(self) -> None:
        """Interrupt pending runs before the application closes its runtime credentials."""
        self._closed = True
        tasks = list(self._tasks.items())
        for run_id, task in tasks:
            self._stop_requested.add(run_id)
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        chat_runs = {(run_id, self.store.get(run_id)["agent_id"]) for run_id, _ in tasks}
        chat_runs.update(
            (record["run_id"], record["agent_id"])
            for record in self.app.sandbox.sessions_for_kind("chat")
            if record["status"] in {"running", "cancelling", "unknown"}
            and record.get("run_id") and record.get("agent_id")
        )
        for run_id, agent_id in chat_runs:
            sessions = await self.app.sandbox.stop_run_sessions(
                agent_id, run_id, "chat", timeout=5.0
            )
            unresolved = any(
                item["status"] in {"running", "cancelling", "unknown"} for item in sessions
            )
            if unresolved:
                self._mark_command_uncertain(run_id, "shutdown_cleanup_unconfirmed")
                if self.store.get(run_id)["status"] == "running":
                    self._fail(run_id, "command_cleanup_unknown", status="failed")
            else:
                if self.store.get(run_id)["status"] == "running":
                    self._fail(run_id, "interrupted", status="interrupted")
            self._stop_requested.discard(run_id)
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _finish_task(self, run_id: str, finished=None) -> None:
        if finished is not None and self._tasks.get(run_id) is not finished:
            return
        self._tasks.pop(run_id, None)
        self._control.pop(run_id, None)
        self._states.pop(run_id, None)
        self._evidence.pop(run_id, None)

    async def _execute(
        self,
        run_id: str,
        profile: ModelProfile,
        credentials: RuntimeCredentials,
        payload: dict,
        effort: str | None,
        requested_effort: str | None,
        recovery: dict | None = None,
        admission: dict | None = None,
    ) -> None:
        from lingnexus.common.diagnostics import diagnostic

        stage = "initialization"
        try:
            if self._client is None:
                self._client = httpx.AsyncClient()
            run = self.store.get(run_id)
            from lingnexus.work_events.service import scope_snapshot

            scope = ChatToolScope(self.app, run["agent_id"], run_id)
            state = {
                "profile": profile,
                "credentials": credentials,
                "payload": payload,
                "effort": effort,
                "requested_effort": requested_effort,
                "step": 1,
                "phase": "ready",
                "initial_length": len(payload.get("input", payload.get("messages", []))),
                "input_guard": input_guard.capture(self.app, run),
                "source_grants": scope_snapshot(self.app, run["agent_id"]),
            }
            if recovery:
                state.update({key: recovery[key] for key in (
                    "payload", "effort", "step", "phase", "initial_length", "pending", "step_limit", "input_guard", "source_grants", "event_projection",
                    "context_epoch", "epoch_step", "replay_prefix", "private_chain", "applied_guidance"
                ) if key in recovery})
            elif admission:
                state.update(admission)
            self._states[run_id] = state
            # Discovery can block on first use (spawning servers, handshakes);
            # keep it off the event loop like the remote calls themselves.
            declare_tools(
                state["payload"], profile, await asyncio.to_thread(self.dispatcher.schema)
            )
            max_steps = state.get("step_limit", self._max_steps())
            step = state["step"]
            while True:
                if self.store.get(run_id)["status"] != "running":
                    return
                await self._control_gate(run_id, state)
                if self.store.get(run_id)["status"] != "running":
                    return
                if step > max_steps:
                    self.store.append(
                        run_id,
                        "notice",
                        {
                            "run_id": run_id,
                            "code": "step_budget_exhausted",
                            "max_steps": max_steps,
                            "message": f"已达到步数上限（{max_steps} 步），任务尚未完成。",
                        },
                    )
                    self._fail(run_id, "step_budget_exhausted")
                    return
                if state.get("phase") == "pending_tools":
                    stage = "tools"
                    calls = [ToolCall(**item) for item in state["pending"]]
                    results = await self._run_tools(run_id, step, calls, scope, run["conversation_id"])
                    append_tool_results(state["payload"], state["profile"],
                                        [(call, text) for call, text, _, _ in results])
                    step += 1
                    state.update(step=step, phase="ready", pending=[])
                    self._ready_checkpoint(run_id, state)
                    continue
                # A recovered inflight HTTP request retries the exact saved input;
                # only ready phases can start a different native chain.
                if state.get("phase") == "ready":
                    self._ready_checkpoint(run_id, state)
                self.store.append(run_id, "step_started", {"run_id": run_id, "step": step})
                stage = "checkpoint"
                state.update(step=step, phase="model_inflight")
                self._save_checkpoint(run_id, state)
                try:
                    final_body = _request_body(state["profile"], state["payload"], state["effort"])
                except ProtocolError:
                    # Preserve gateway's own typed rejection for an invalid
                    # capability/configuration; this branch only estimates size.
                    final_body = state["payload"]
                if self.app.context.counter.count(json.dumps(final_body, ensure_ascii=False)) + (
                    state["profile"].max_output_tokens or 4096
                ) > (state["profile"].context_window_tokens or 32768):
                    self._fail(run_id, "context_limit")
                    return
                stage = "model"
                turn = await self._step_call(run_id, step, state, requested_effort)
                if turn is None:
                    return
                if turn.continuation is not None:
                    encoded = json.dumps(turn.continuation, ensure_ascii=False)
                    if state["credentials"].api_key in encoded:
                        raise CredentialEcho()
                    append_native_response(state["payload"], state["profile"].api_family, turn.continuation)
                elif turn.calls:
                    append_tool_calls(state["payload"], state["profile"], turn.calls, turn.text)
                if turn.text and turn.calls:
                    self.app.work_events.record(run_id, "assistant_plan", turn.text, step=step,
                                                metadata={"scope_grants": state["source_grants"]})
                if not turn.calls:
                    stage = "completion"
                    if turn.finish_reason in COMPLETE_REASONS[state["profile"].api_family]:
                        await self._before_turn_end(run_id, turn.text)
                        await self._complete(run_id, turn.text)
                    else:
                        self._fail(run_id, "incomplete_response")
                    return
                state.update(phase="pending_tools", pending=[{
                    "id": call.id, "name": call.name, "arguments": call.arguments,
                    "arguments_json": call.arguments_json} for call in turn.calls])
                self._save_checkpoint(run_id, state)
                stage = "tools"
                results = await self._run_tools(
                    run_id, step, turn.calls, scope, run["conversation_id"]
                )
                append_tool_results(
                    state["payload"],
                    state["profile"],
                    [(call, text) for call, text, _, _ in results],
                )
                step += 1
                state.update(step=step, phase="ready", pending=[])
                self._ready_checkpoint(run_id, state)
        except CompactionBlocked as exc:
            self._fail(run_id, exc.code)
        except CredentialEcho:
            self._fail(run_id, "credential_in_response")
        except FailoverBlocked as exc:
            self._fail(run_id, exc.code)
        except CheckpointLimit:
            self._fail(run_id, "checkpoint_limit")
        except asyncio.CancelledError:
            if run_id not in self._stop_requested:
                self._fail(run_id, "interrupted", status="interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 - background boundary must sanitize every failure
            # Never serialize/log exception strings or provider continuation.
            detail = diagnostic(stage, exc)
            self.store.append(run_id, "notice", {"run_id": run_id, "code": "runtime_diagnostic",
                **detail, "message": "运行故障，诊断编号：" + detail["diagnostic_id"]})
            self._fail(run_id, "runtime_error")

    async def _control_gate(self, run_id: str, state: dict) -> None:
        """Honor pause/resume and inject guidance — only ever at a step boundary."""
        control = self._control.get(run_id)
        if control is None:
            return
        if control.pause_requested:
            self.store.append(run_id, "paused", {"run_id": run_id})
            while control.pause_requested:
                control.resume_event.clear()
                await control.resume_event.wait()
            self.store.append(run_id, "resumed", {"run_id": run_id})
        while control.guides:
            guidance = control.guides.pop(0)
            self._assert_input_current(run_id)
            self.app.work_events.record(run_id, "guidance_applied", guidance,
                step=state["step"], call_id=new_id(), metadata={"scope_grants": state["source_grants"]})
            append_user_message(state["payload"], state["profile"], guidance)
            state.setdefault("applied_guidance", []).append(guidance)

    async def _step_call(
        self, run_id: str, step: int, state: dict, requested_effort: str | None
    ) -> _Turn | None:
        """Run one model call; step one keeps the primary→backup restart policy.

        Every step records its own attempt row, so the accounting is honest for
        the whole loop — a second or third model call is never dropped.
        """
        if step > 1:
            profile_id = self.store.get(run_id)["profile_id"]
            attempt = self.failover.begin_attempt(run_id, step, 1, profile_id)
            turn = await self._stream_turn(
                run_id,
                step,
                state["profile"],
                state["payload"],
                state["credentials"],
                state["effort"],
                attempt=attempt,
            )
            if turn is None:
                return None
            if turn.eof:
                self.failover.finish_attempt(run_id, "failed", "unexpected_eof")
                self._fail(run_id, "unexpected_eof")
                return None
            if turn.error is not None:
                error = turn.error.error
                code = error.code if error else "runtime_error"
                self.failover.finish_attempt(
                    run_id, "failed", code, error.status_code if error else None
                )
                self._fail(run_id, code)
                return None
            self.failover.finish_attempt(run_id, "completed")
            return turn
        for attempt in (1, 2):
            if self.store.get(run_id)["status"] != "running":
                return None
            profile_id = self.store.get(run_id)["profile_id"]
            attempt = self.failover.begin_attempt(run_id, step, attempt, profile_id)
            turn = await self._stream_turn(
                run_id,
                step,
                state["profile"],
                state["payload"],
                state["credentials"],
                state["effort"],
                attempt=attempt,
            )
            if turn is None:
                return None
            if turn.eof:
                self.failover.finish_attempt(run_id, "failed", "unexpected_eof")
                self._fail(run_id, "unexpected_eof")
                return None
            if turn.error is None:
                self.failover.finish_attempt(run_id, "completed")
                return turn
            error = turn.error.error
            code = error.code if error else "runtime_error"
            self.failover.finish_attempt(
                run_id, "failed", code, error.status_code if error else None
            )
            replacement = self.failover.retry(run_id, turn.error)
            if replacement is None:
                self._fail(run_id, code)
                return None
            profile_id, profile, credentials, payload = replacement
            state.update(profile=profile, credentials=credentials, payload=payload)
            state["initial_length"] = len(payload.get("input", payload.get("messages", [])))
            state["effort"] = self.failover.effort(run_id, profile_id, profile, requested_effort)
            declare_tools(
                state["payload"], profile, await asyncio.to_thread(self.dispatcher.schema)
            )
            # Failover changes the active profile and rebuilt payload. Persist
            # the coherent model_inflight state before starting the backup request.
            self._save_checkpoint(run_id, state)
        return None

    async def _stream_turn(
        self,
        run_id: str,
        step: int,
        profile: ModelProfile,
        payload: dict,
        credentials: RuntimeCredentials,
        effort: str | None,
        attempt: int,
    ) -> _Turn | None:
        """One native model stream reduced to text, tool calls and a finish marker."""
        text: list[str] = []
        calls: list[ToolCall] = []
        guards = {
            "text": SecretGuard(credentials.api_key),
            "thinking": SecretGuard(credentials.api_key),
        }
        gateway = ModelGateway(self._client)
        self._assert_input_current(run_id)
        async with aclosing(
            gateway.stream(profile, payload, credentials=credentials, effort=effort)
        ) as stream:
            async for event in stream:
                if self.store.get(run_id)["status"] != "running":
                    return None
                self.failover.usage(run_id, step, attempt, event.usage)
                if event.type in ("text_delta", "thinking_delta"):
                    kind = "text" if event.type == "text_delta" else "thinking"
                    safe_text = guards[kind].feed(event.text or "")
                    if kind == "text":
                        text.append(safe_text)
                    if safe_text:
                        self.store.append(run_id, kind, {"run_id": run_id, "text": safe_text})
                elif event.type == "tool_call":
                    if event.tool_call is not None:
                        calls.append(event.tool_call)
                elif event.type == "tool_call_delta":
                    continue
                elif event.type == "error":
                    from lingnexus.common.diagnostics import diagnostic

                    detail = diagnostic("model", reason=event.error.reason if event.error else None)
                    self.store.append(run_id, "notice", {"run_id": run_id, "code": "runtime_diagnostic",
                        **detail, "message": "模型通信失败，诊断编号：" + detail["diagnostic_id"]})
                    return _Turn(text="".join(text), calls=calls, error=event)
                elif event.type == "complete":
                    for channel, guard in guards.items():
                        remaining = guard.finish()
                        if remaining:
                            self.store.append(run_id, channel, {"run_id": run_id, "text": remaining})
                            if channel == "text":
                                text.append(remaining)
                    return _Turn(
                        text="".join(text), calls=calls, finish_reason=event.finish_reason,
                        continuation=event.continuation.response if event.continuation else None,
                    )
        return _Turn(text="".join(text), calls=calls, eof=True)

    async def _run_tools(
        self,
        run_id: str,
        step: int,
        calls: list[ToolCall],
        scope: ChatToolScope,
        conversation_id: str,
    ) -> list[tuple[ToolCall, str, str, bool]]:
        """Run this step's calls in request order; adjacent read-only calls fan out.

        Every call still passes BEFORE_TOOL → dispatch → AFTER_TOOL, and the
        started/result events are appended in request order, so a run always
        replays as one deterministic transcript whatever order work finished in.
        The four-tuple bundles the full-access release marker with the ordinary
        result so the event record can carry it.
        """
        outcomes: dict[int, tuple[ToolCall, str, str, bool]] = {}
        for batch in _call_batches(calls):
            for call, _ in batch:
                self._emit_tool_started(run_id, step, call)
            if len(batch) == 1:
                call, position = batch[0]
                outcomes[position] = await self._invoke_tool(
                    run_id, step, call, scope, conversation_id
                )
            else:
                settled = await asyncio.gather(
                    *(
                        self._invoke_tool(run_id, step, call, scope, conversation_id)
                        for call, _ in batch
                    )
                )
                for (_, position), outcome in zip(batch, settled):
                    outcomes[position] = outcome
            for call, position in batch:
                _, text, status, auto_full = outcomes[position]
                self._emit_tool_result(run_id, step, call, text, status, auto_full)
        return [outcomes[position] for position in range(len(calls))]

    async def _invoke_tool(
        self,
        run_id: str,
        step: int,
        call: ToolCall,
        scope: ChatToolScope,
        conversation_id: str,
    ) -> tuple[ToolCall, str, str, bool]:
        """One tool call through BEFORE_TOOL → dispatch → AFTER_TOOL.

        A rejected call never runs; its reason travels back to the model as the
        tool result so it can adapt. The permission decision lives entirely in the
        hooks, never here — including the full-access release: the hook carries
        the marker back through its result and the loop only records it. The call
        identity is environment plumbing for the scope (a deterministic effect
        id), never a policy decision.
        """
        self._assert_input_current(run_id)
        previous = self.checkpoints.effect(run_id, step, call.id)
        if previous:
            if previous["status"] != "completed":
                raise Conflict("工具副作用未知，不能自动重放。")
            text, status, auto_full = previous["result"]
            return call, text, status, auto_full
        bound = scope.bound(step, call.id)
        context = {
            "agent_id": bound.agent_id,
            "command_available": bound.command_available,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "step": step,
            "call_id": call.id,
        }
        started = time.monotonic()
        decision = await self.app.hooks.run(
            HookEvent.BEFORE_TOOL,
            {"name": call.name, "arguments": call.arguments, "context": context},
        )
        if not decision.allow:
            reason = decision.reason or "该工具调用被策略拒绝。"
            text = json.dumps(
                {"blocked": True, "tool": call.name, "reason": reason}, ensure_ascii=False
            )
            return call, text, "blocked", False
        auto_full = bool(decision.replacement and decision.replacement.get("full_access"))
        self._assert_input_current(run_id)
        self.checkpoints.begin(run_id, step, call.id)
        if call.name in {"event_records", "event_read"} and str(call.arguments.get("event_id", "")).startswith("run:"):
            result = self.compaction.recall(run_id, call.name, call.arguments)
        elif call.name in {"create_file", "write_file", "edit_file", "run_command"}:
            async with self.execution_leases.guard(f"chat:{bound.agent_id}:{run_id}") as fence:
                self._assert_input_current(run_id)
                result = await self.dispatcher.execute(call.name, call.arguments, bound)
                fence.update(result_fence(result.data))
        else:
            result = await self.dispatcher.execute(call.name, call.arguments, bound)
        self._assert_input_current(run_id)
        account(self._evidence.setdefault(run_id, initial(coding=True)),
                {"name": call.name, "arguments": call.arguments}, result.data)
        text = _tool_text(result)
        credentials = self._states[run_id]["credentials"]
        if credentials.api_key in text:
            self.checkpoints.finish(run_id, step, call.id, ["sensitive_result_discarded", "unknown", auto_full])
            raise CredentialEcho()
        self.checkpoints.finish(run_id, step, call.id, [text, result.status, auto_full])
        # The safe full result must survive even when the growing private native
        # checkpoint crosses its separate 8 MiB cap immediately after this call.
        self._capture_tool_result(run_id, step, call, text, result.status)
        self._save_checkpoint(run_id, self._states[run_id])
        await self.app.hooks.run(
            HookEvent.AFTER_TOOL,
            {
                "name": call.name,
                "arguments": call.arguments,
                "status": result.status,
                "content": text,
                "started_at": started,
                "context": context,
            },
        )
        return call, text, result.status, auto_full

    def _emit_tool_started(self, run_id: str, step: int, call: ToolCall) -> None:
        self._assert_input_current(run_id)
        state = self._states[run_id]
        if state["credentials"].api_key in call.arguments_json:
            raise CredentialEcho()
        self.app.work_events.record(run_id, "tool_call", call.arguments_json, step=step, call_id=call.id,
            metadata={"name": call.name, "scope_grants": state["source_grants"]})
        self.store.append(
            run_id,
            "tool_started",
            {
                "run_id": run_id,
                "step": step,
                "name": call.name,
                "call_id": call.id,
                "arguments": call.arguments,
            },
        )

    def _emit_tool_result(
        self,
        run_id: str,
        step: int,
        call: ToolCall,
        text: str,
        status: str,
        auto_full: bool,
    ) -> None:
        self._capture_tool_result(run_id, step, call, text, status)
        data = {
            "run_id": run_id,
            "step": step,
            "name": call.name,
            "call_id": call.id,
            "status": status,
            "preview": _preview(text),
            "verification": public(self._evidence.get(run_id, {})),
        }
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict) and isinstance(decoded.get("execution"), dict):
                data["execution"] = decoded["execution"]
        except (ValueError, TypeError):
            pass
        if auto_full:
            data["access"] = "full"
        self.store.append(run_id, "tool_result", data)

    def _capture_tool_result(self, run_id, step, call, text, status):
        state = self._states[run_id]
        if state["credentials"].api_key in text:
            raise CredentialEcho()
        bounded = False
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                bounded = bool(decoded.get("truncated") or decoded.get("output_truncated") or decoded.get("execution", {}).get("output_truncated"))
        except (ValueError, TypeError, AttributeError):
            pass
        self.app.work_events.record(run_id, "tool_result", text, step=step, call_id=call.id,
            metadata={"name": call.name, "status": status, "coverage": "bounded" if bounded else "retained", "scope_grants": state["source_grants"]})

    async def _before_turn_end(self, run_id: str, text: str) -> None:
        """Trigger the turn-end mount point before anything is persisted.

        No handler is attached this round; the call is real so future long-term
        memory can hang off it without touching this loop.
        """
        run = self.store.get(run_id)
        await self.app.hooks.run(
            HookEvent.BEFORE_TURN_END,
            {
                "run_id": run_id,
                "conversation_id": run["conversation_id"],
                "agent_id": run["agent_id"],
                "text": text,
            },
        )

    def _max_steps(self) -> int:
        return read_int(
            self.app.database, MAX_STEPS_SETTING, DEFAULT_MAX_STEPS, minimum=1
        )

    async def _complete(self, run_id: str, text: str) -> None:
        self._assert_input_current(run_id)
        run = self.store.get(run_id)
        sessions = self.app.sandbox.run_sessions(run["agent_id"], run_id, "chat")
        active = any(item["status"] in {"running", "cancelling"} for item in sessions)
        unknown = any(item["status"] == "unknown" for item in sessions)
        if active or unknown:
            if active:
                sessions = await self.app.sandbox.stop_run_sessions(
                    run["agent_id"], run_id, "chat", timeout=5.0
                )
            self._mark_command_uncertain(
                run_id, "command_active_at_final" if active else "command_unknown_at_final"
            )
            code = "command_session_unknown" if unknown or any(
                item["status"] in {"running", "cancelling", "unknown"} for item in sessions
            ) else "command_session_active"
            self._fail(run_id, code)
            return
        evidence = self._evidence.setdefault(run_id, initial(coding=True))
        ensure_current(evidence, ChatToolScope(self.app, run["agent_id"], run_id).workdir)
        with self.app.database.transaction() as db:
            run = self.get_run(run_id)
            if run["status"] != "running":
                return
            try:
                conversation = self.failover.validate_head(run)
                message = (
                    self.app.conversations.append_assistant(
                        conversation["id"],
                        text,
                        request_id=f"chat:{run_id}:assistant",
                        expected_head=run["user_message_id"],
                    )
                    if text
                    else None
                )
            except (DomainError, TypeError, ValueError):
                self._fail(run_id, "context_invalidated")
                return
            db.execute(
                "UPDATE chat_runs SET status='completed',finished_at=? WHERE id=?",
                (utc_now(), run_id),
            )
            self.store.append(
                run_id,
                "complete",
                {
                    "run_id": run_id,
                    "status": "completed",
                    "assistant_message_id": message["id"] if message else None,
                    "verification": public(evidence),
                },
            )
            self.failover.finish_attempt(run_id, "completed")
            if state := self._states.get(run_id):
                inherit = getattr(getattr(self.app, "continuity", None), "inherit_answer_dependencies", None)
                if inherit and message:
                    inherit(run_id, message["id"])
                state.update(phase="completed", assistant_message_id=message["id"] if message else None)
                self._save_checkpoint(run_id, state)

    def _fail(self, run_id: str, code: str, *, status: str = "failed") -> None:
        with self.app.database.transaction() as db:
            if self.get_run(run_id)["status"] != "running":
                return
            if code not in ERROR_MESSAGES:
                code = "runtime_error"
            self.failover.finish_attempt(run_id, status, code)
            db.execute(
                "UPDATE chat_runs SET status=?,finished_at=? WHERE id=?",
                (status, utc_now(), run_id),
            )
            self.store.append(
                run_id,
                "error",
                {
                    "run_id": run_id,
                    "code": code,
                    "message": ERROR_MESSAGES[code],
                },
            )
