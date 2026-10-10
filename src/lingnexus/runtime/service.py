"""Chat orchestration without transport/API business logic or provider SDKs."""

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING

import httpx

from lingnexus.common.errors import Conflict, DomainError
from lingnexus.common.types import new_id, utc_now
from lingnexus.models import APIFamily, ModelGateway, ModelProfile, RuntimeCredentials

from . import attachments
from .failover import ChatFailover, FailoverBlocked, context_refs
from .payload import build_payload
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
    "tools_unsupported": "Tools are not supported by chat yet.",
    "credential_in_response": "The service returned sensitive runtime material; this response was discarded.",
    "runtime_error": "The chat run could not be completed.",
    "cancelled": "The owner cancelled this run.",
    "interrupted": "The server stopped before this run completed.",
}

COMPLETE_REASONS = {
    APIFamily.OPENAI_CHAT_COMPLETIONS: ("stop",),
    APIFamily.ANTHROPIC_MESSAGES: ("end_turn", "stop_sequence"),
    APIFamily.OPENAI_RESPONSES: ("completed",),
}


class ChatRuntime:
    """One manager per database/process, used on the server's asyncio loop.

    submit(ChatRequest) atomically saves a user source and a run, then schedules
    the model independently of subscribers. It returns a run dict with id,
    status, profile_id, conversation_id, user_message_id, self_revision,
    user_source, assistant_source, context_sources and context_manifest. New fields are null on
    legacy runs; sources and original events stay replayable without backfill.
    A repeated (agent_id, request_id) with identical input returns that run,
    including terminal runs, without another model call or requiring credentials.
    Different input or an overlapping turn for that agent raises Conflict (409).

    events(run_id, after=0) yields durable {id, event, data} records. id is a
    positive per-run sequence, and after is exclusive. started carries
    {run_id, conversation_id};
    text/thinking carry {run_id, text}; usage carries
    {run_id, attempt, profile_id, snapshot: true, usage}; complete
    carries {run_id, status: completed, assistant_source}; error carries
    {run_id, code, message}. Only complete/error end a subscriber. Thinking is
    provider-returned visible display material, never an assistant source.
    notice carries a safe code/message; model_switch carries
    {run_id, from_profile, to_profile, restart: true}. Subscribers must reset
    both visible buffers at that marker. Earlier partial events stay replayable.

    cancel(run_id) is idempotent and terminal. Closing/cancelling events() only
    detaches its subscriber. aclose() interrupts active runs and closes owned
    clients; injected clients remain caller-owned. Construction recovers rows
    left running after a process crash as interrupted. No crash reissue is
    attempted. Unavailable main calls can restart once on the designated backup;
    that conversation retains its backup binding across subsequent turns.

    Only the selected conversation branch supplies transcript sources. ID-less
    clients use a persistent default real conversation. ContextBuilder supplies
    current self instructions without resetting personality on new conversations.
    Completion and failover revalidate sources (including recall), selected head
    and self revision atomically. Native continuation is never persisted/replayed.
    Regeneration selects/reuses a matching user node on the current branch.
    Attachments are linked to that user node with immutable digests. Only files
    in included transcript nodes are sent; their bytes are checked again before
    assistant persistence. Replay uses durable input and never rereads files.
    """

    def __init__(self, application: "Application", *, client: httpx.AsyncClient | None = None):
        self.app = application
        self.store = RunStore(application.database)
        self.failover = ChatFailover(application, self.store)
        self._client = client
        self._owns_client = client is None
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False
        with self.app.database.transaction() as db:
            pending = db.execute("SELECT id FROM chat_runs WHERE status='running'").fetchall()
            for row in pending:
                self._fail(row["id"], "interrupted", status="interrupted")

    def get_run(self, run_id: str) -> dict:
        """Return durable status and source references, without model/credential internals."""
        with self.app.database.transaction():
            return self.store.get(run_id) | self.failover.metadata(run_id)

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
            if db.execute(
                "SELECT 1 FROM chat_runs WHERE agent_id=? AND status='running'",
                (value.agent_id,),
            ).fetchone():
                raise Conflict("This agent already has a running chat turn.")
            run_id = new_id()
            conversation = (
                self.app.conversations.for_chat(value.agent_id, value.conversation_id)
                if value.conversation_id is not None
                else self.app.conversations.default_real(value.agent_id)
            )
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
            user_ref = user["source"]
            refs = [node["source"] for node in self.app.conversations.messages(conversation["id"])]
            payload, manifest = build_payload(
                self.app, value.agent_id, profile, refs
            )
            db.execute(
                "INSERT INTO chat_runs(id,agent_id,request_id,fingerprint,profile_id,status,"
                "user_source_id,user_source_revision,context_sources,created_at,"
                "conversation_id,user_message_id,self_revision,context_manifest) "
                "VALUES (?,?,?,?,?,'running',?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    value.agent_id,
                    value.request_id,
                    fingerprint,
                    profile_id,
                    user_ref.id,
                    user_ref.revision,
                    json.dumps([r.model_dump() for r in context_refs(refs, manifest)]),
                    utc_now(),
                    conversation["id"],
                    user["id"],
                    manifest["self_revision"],
                    json.dumps(manifest),
                ),
            )
            self.store.append(
                run_id, "started", {"run_id": run_id, "conversation_id": conversation["id"]}
            )
            self.failover.save_selection(run_id, selected, refs)
            effort = self.failover.effort(run_id, profile_id, profile, value.effort)
        task = loop.create_task(
            self._execute(run_id, profile, selected.credentials, payload, effort, value.effort)
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
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

    def cancel(self, run_id: str) -> dict:
        """Explicit owner stop. Cancelling a terminal run leaves its result untouched."""
        self._fail(run_id, "cancelled", status="cancelled")
        if task := self._tasks.get(run_id):
            task.cancel()
        return self.get_run(run_id)

    async def aclose(self) -> None:
        """Interrupt pending runs before the application closes its runtime credentials."""
        self._closed = True
        tasks = list(self._tasks.items())
        for run_id, task in tasks:
            self._fail(run_id, "interrupted", status="interrupted")
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _execute(
        self,
        run_id: str,
        profile: ModelProfile,
        credentials: RuntimeCredentials,
        payload: dict,
        effort: str | None,
        requested_effort: str | None,
    ) -> None:
        try:
            if self._client is None:
                self._client = httpx.AsyncClient()
            profile_id = self.store.get(run_id)["profile_id"]
            # Exactly two possible native calls; each has its own gateway/parser.
            for attempt in (1, 2):
                if self.store.get(run_id)["status"] != "running":
                    return
                self.failover.begin_attempt(run_id, attempt, profile_id)
                text: list[str] = []
                guards = {"text":SecretGuard(credentials.api_key),"thinking":SecretGuard(credentials.api_key)}
                failure = None
                gateway = ModelGateway(self._client)
                async with aclosing(
                    gateway.stream(profile, payload, credentials=credentials, effort=effort)
                ) as stream:
                    async for event in stream:
                        if self.store.get(run_id)["status"] != "running":
                            return
                        self.failover.usage(run_id, attempt, event.usage)
                        if event.type in ("text_delta", "thinking_delta"):
                            kind = "text" if event.type == "text_delta" else "thinking"
                            safe_text = guards[kind].feed(event.text or "")
                            if kind == "text":
                                text.append(safe_text)
                            if safe_text:
                                self.store.append(run_id,kind,{"run_id":run_id,"text":safe_text})
                        elif event.type in ("tool_call", "tool_call_delta"):
                            self._fail(run_id, "tools_unsupported")
                            return
                        elif event.type == "error":
                            failure = event
                            break
                        elif event.type == "complete":
                            for channel,guard in guards.items():
                                remaining=guard.finish()
                                if remaining:
                                    self.store.append(run_id,channel,{"run_id":run_id,"text":remaining})
                                    if channel=="text":text.append(remaining)
                            if event.finish_reason in ("tool_calls", "tool_use", "function_call"):
                                self._fail(run_id, "tools_unsupported")
                            elif event.finish_reason in COMPLETE_REASONS[profile.api_family]:
                                self._complete(run_id, "".join(text))
                            else:
                                self._fail(run_id, "incomplete_response")
                            return
                if failure is None:
                    self._fail(run_id, "unexpected_eof")
                    return
                code = failure.error.code if failure.error else "runtime_error"
                self.failover.finish_attempt(
                    run_id,
                    "failed",
                    code,
                    failure.error.status_code if failure.error else None,
                )
                replacement = self.failover.retry(run_id, failure)
                if replacement is None:
                    self._fail(run_id, code)
                    return
                profile_id, profile, credentials, payload = replacement
                effort = self.failover.effort(run_id, profile_id, profile, requested_effort)
        except CredentialEcho:
            self._fail(run_id,"credential_in_response")
        except FailoverBlocked as exc:
            self._fail(run_id, exc.code)
        except asyncio.CancelledError:
            self._fail(run_id, "interrupted", status="interrupted")
            raise
        except Exception:  # noqa: BLE001 - background boundary must sanitize every failure
            # Never serialize/log exception strings or provider continuation.
            self._fail(run_id, "runtime_error")

    def _complete(self, run_id: str, text: str) -> None:
        with self.app.database.transaction() as db:
            run = self.get_run(run_id)
            if run["status"] != "running":
                return
            try:
                conversation, _ = self.failover.validate_context(run)
                attachments.validate_manifest(
                    self.app, run["agent_id"], run["context_manifest"] or {}
                )
                ref = (
                    self.app.conversations.append_assistant(
                        conversation["id"],
                        text,
                        request_id=f"chat:{run_id}:assistant",
                        expected_head=run["user_message_id"],
                    )["source"]
                    if text
                    else None
                )
            except (DomainError, TypeError, ValueError):
                self._fail(run_id, "context_invalidated")
                return
            db.execute(
                "UPDATE chat_runs SET status='completed',assistant_source_id=?,"
                "assistant_source_revision=?,finished_at=? WHERE id=?",
                (ref.id if ref else None, ref.revision if ref else None, utc_now(), run_id),
            )
            self.store.append(
                run_id,
                "complete",
                {
                    "run_id": run_id,
                    "status": "completed",
                    "assistant_source": ref.model_dump() if ref else None,
                },
            )
            self.failover.finish_attempt(run_id, "completed")

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
