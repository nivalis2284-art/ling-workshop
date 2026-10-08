"""CHAT-only selection, bounded restart policy and durable per-call accounting.

The gateway owns native continuation. Only error *identifiers* are inspected in
memory; no native object, signature, provider message or key reaches this store.
Export CHAT_FAILOVER_TABLES for accounting/maintenance integration.
"""

import json
from dataclasses import dataclass

from lingnexus.common.errors import Conflict, ContextTooLarge, DomainError, PermissionDenied
from lingnexus.common.types import SourceRef, utc_now
from lingnexus.identity.policy import validate_experience
from lingnexus.memory.maintenance import MemoryMaintenance
from lingnexus.models import ModelEvent, ModelProfile, RuntimeCredentials, Usage

from .payload import build_payload

CHAT_FAILOVER_TABLES = (
    "chat_conversation_failover",
    "chat_run_profiles",
    "chat_call_attempts",
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
)
UNAVAILABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
UNAVAILABLE_PROVIDER = frozenset(
    {
        "overloaded_error",
        "overloaded",
        "rate_limit_error",
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
        "temporarily_unavailable",
        "model_unavailable",
    }
)


def unavailable(event: ModelEvent) -> bool:
    """Fail closed: malformed, refusal, truncation and unknown errors never retry."""
    error = event.error
    if error is None:
        return False
    # A connection can die AFTER a refusal/length/tool finish was received.
    # That remains a semantic failure, even if the final SSE marker is missing.
    response = event.continuation.response if event.continuation else {}
    reasons = [event.finish_reason, response.get("stop_reason")]
    choices = response.get("choices")
    if isinstance(choices, list):
        reasons.extend(c.get("finish_reason") for c in choices if isinstance(c, dict))
        if any(
            isinstance(c, dict)
            and isinstance(c.get("message"), dict)
            and c["message"].get("refusal")
            for c in choices
        ):
            return False
    if response.get("status") == "incomplete" or any(
        r is not None and r not in ("stop", "end_turn", "stop_sequence", "completed")
        for r in reasons
    ):
        return False
    if error.code in ("transport_error", "timeout", "unexpected_eof"):
        return True
    if error.code == "http_error":
        return error.status_code in UNAVAILABLE_HTTP
    if error.code != "provider_error" or event.continuation is None:
        return False
    # The terminal native event can identify overload without exposing its text.
    native = event.continuation.events[-1] if event.continuation.events else {}
    value = native.get("error")
    response = native.get("response")
    if value is None and isinstance(response, dict):
        value = response.get("error")
    if not isinstance(value, dict):
        return False
    identifiers = [value[k] for k in ("code", "type") if value.get(k) is not None]
    return bool(identifiers) and all(
        isinstance(v, str) and v in UNAVAILABLE_PROVIDER for v in identifiers
    )


def context_refs(refs: list[SourceRef], manifest: dict) -> list[SourceRef]:
    """Recall is validation evidence, not a message in the conversation tree."""
    values = [*refs, *(SourceRef.model_validate(v) for v in manifest.get("recall_sources", []))]
    return list({(r.id, r.revision): r for r in values}.values())


@dataclass(frozen=True)
class Selection:
    requested: str | None
    initial: str
    main: str | None
    fallback: str | None
    profile_id: str
    profile: ModelProfile
    credentials: RuntimeCredentials
    reason: str


class FailoverBlocked(DomainError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ChatFailover:
    def __init__(self, app, store):
        self.app, self.store = app, store

    def select(self, requested: str | None, conversation_id: str) -> Selection:
        settings = self.app.profiles.selection()
        main, fallback = settings["primary"], settings["fallback"]
        pid = requested or main
        if not pid:
            raise DomainError("Select a primary model or supply profile_id before chatting.")
        reason = "manual" if requested and requested != main else "primary"
        with self.app.database.transaction() as db:
            binding = db.execute(
                "SELECT backup_profile_id FROM chat_conversation_failover WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if reason != "manual" and binding:
            pid, reason = binding[0], "conversation_backup"
        initial = pid
        try:
            profile, credentials = self.app.profiles.resolve(pid)
        except PermissionDenied:
            if reason != "primary" or not fallback or fallback == pid:
                raise
            pid, reason = fallback, "credentials_failover"
            profile, credentials = self.app.profiles.resolve(pid)
        if not profile.capabilities.streaming:
            raise DomainError("The selected model does not support streaming.")
        return Selection(requested, initial, main, fallback, pid, profile, credentials, reason)

    def save_selection(self, run_id: str, selected: Selection, refs: list[SourceRef]) -> None:
        with self.app.database.transaction() as db:
            db.execute(
                "INSERT INTO chat_run_profiles VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    selected.requested,
                    selected.initial,
                    selected.main,
                    selected.fallback,
                    selected.reason,
                    json.dumps([r.model_dump() for r in refs]),
                ),
            )
            if selected.reason == "credentials_failover":
                self._notice(
                    run_id, selected.initial, selected.profile_id, "credentials_unavailable"
                )
                self._switch(run_id, selected.initial, selected.profile_id)

    def effort(self, run_id: str, profile_id: str, profile: ModelProfile, requested: str | None):
        if requested is not None and requested not in profile.capabilities.effort_levels:
            self.store.append(
                run_id,
                "notice",
                {
                    "run_id": run_id,
                    "code": "effort_omitted",
                    "profile_id": profile_id,
                    "requested_effort": requested,
                    "message": "The backup does not support this effort level; using its default.",
                },
            )
            return None
        return requested

    def validate_context(self, run: dict) -> tuple[dict, list[SourceRef]]:
        conversation = self.app.conversations.for_chat(run["agent_id"], run["conversation_id"])
        if conversation["head_id"] != run["user_message_id"]:
            raise Conflict("The selected conversation head changed.")
        if self.app.identity.current(run["agent_id"]).revision != run["self_revision"]:
            raise Conflict("The current self state changed.")
        refs = [SourceRef.model_validate(v) for v in run["context_sources"]]
        MemoryMaintenance(self.app).validate_markers(
            run["agent_id"], (run["context_manifest"] or {}).get("memory_refs", [])
        )
        self._validate_sources(run["agent_id"], refs, conversation, run["context_manifest"] or {})
        return conversation, refs

    def _validate_sources(
        self, agent_id: str, refs: list[SourceRef], conversation: dict, manifest: dict
    ):
        recalled = {(r["id"], r["revision"]) for r in manifest.get("recall_sources", [])}
        # A transcript reference always retains exact scope, even if it also
        # occurs in recall provenance. External user messages cannot become real.
        transcript = {
            (n["source"].id, n["source"].revision)
            for n in self.app.conversations.messages(conversation["id"])
        }
        recall_refs, conversation_refs = [], []
        for ref in refs:
            key = (ref.id, ref.revision)
            (
                recall_refs if key in recalled and key not in transcript else conversation_refs
            ).append(ref)
        if conversation_refs:
            self.app.history.validate_refs(agent_id, conversation_refs, scope=conversation["scope"])
        if recall_refs:
            validate_experience(self.app.history, agent_id, recall_refs)

    def retry(self, run_id: str, event: ModelEvent):
        """Return a fresh backup call or None; all switch effects commit atomically."""
        error = event.error
        if error is None or not unavailable(event):
            return None
        with self.app.database.transaction() as db:
            run = self.store.get(run_id)
            policy = db.execute(
                "SELECT * FROM chat_run_profiles WHERE run_id=?", (run_id,)
            ).fetchone()
            count = db.execute(
                "SELECT COUNT(*) FROM chat_call_attempts WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            backup = policy["fallback_profile_id"]
            if (
                run["status"] != "running"
                or count != 1
                or policy["selection_reason"] != "primary"
                or run["profile_id"] != policy["main_profile_id"]
                or not backup
                or backup == run["profile_id"]
            ):
                return None
            try:
                conversation, _ = self.validate_context(run)
                refs = [
                    SourceRef.model_validate(v) for v in json.loads(policy["conversation_sources"])
                ]
            except (DomainError, TypeError, ValueError):
                raise FailoverBlocked("context_invalidated") from None
            try:
                profile, credentials = self.app.profiles.resolve(backup)
            except PermissionDenied:
                raise FailoverBlocked("credentials_unavailable") from None
            except DomainError:
                raise FailoverBlocked("profile_unavailable") from None
            if not profile.capabilities.streaming:
                raise FailoverBlocked("profile_unavailable")
            try:
                payload, manifest = build_payload(
                    self.app,
                    run["agent_id"],
                    profile,
                    refs,
                    scope=conversation["scope"],
                )
                if manifest["self_revision"] != run["self_revision"]:
                    raise Conflict("The current self state changed.")
                checked_refs = context_refs(
                    refs,
                    manifest,
                )
                self.validate_context(run)
                self._validate_sources(run["agent_id"], checked_refs, conversation, manifest)
            except ContextTooLarge:
                raise FailoverBlocked("context_unavailable") from None
            except (DomainError, TypeError, ValueError):
                raise FailoverBlocked("context_invalidated") from None
            db.execute(
                "UPDATE chat_runs SET context_sources=?,context_manifest=? WHERE id=?",
                (json.dumps([r.model_dump() for r in checked_refs]), json.dumps(manifest), run_id),
            )
            self._notice(run_id, run["profile_id"], backup, error.code)
            self._switch(run_id, run["profile_id"], backup)
            return backup, profile, credentials, payload

    def _notice(self, run_id: str, previous: str, backup: str, reason: str) -> None:
        self.store.append(
            run_id,
            "notice",
            {
                "run_id": run_id,
                "code": "model_unavailable",
                "reason": reason,
                "from_profile": previous,
                "to_profile": backup,
                "message": "The main model is unavailable; switching this conversation to its backup.",
            },
        )

    def _switch(self, run_id: str, previous: str, backup: str) -> None:
        with self.app.database.transaction() as db:
            run = self.store.get(run_id)
            db.execute(
                "INSERT INTO chat_conversation_failover VALUES (?,?,?,?,?) "
                "ON CONFLICT(conversation_id) DO NOTHING",
                (run["conversation_id"], previous, backup, run_id, utc_now()),
            )
            db.execute("UPDATE chat_runs SET profile_id=? WHERE id=?", (backup, run_id))
            self.store.append(
                run_id,
                "model_switch",
                {
                    "run_id": run_id,
                    "from_profile": previous,
                    "to_profile": backup,
                    "restart": True,
                },
            )

    def begin_attempt(self, run_id: str, attempt: int, profile_id: str) -> None:
        with self.app.database.transaction() as db:
            db.execute(
                "INSERT INTO chat_call_attempts(run_id,attempt,profile_id,status,started_at) "
                "VALUES (?,?,?,'running',?)",
                (run_id, attempt, profile_id, utc_now()),
            )

    def finish_attempt(self, run_id: str, status: str, code: str | None = None, http_status=None):
        with self.app.database.transaction() as db:
            db.execute(
                "UPDATE chat_call_attempts SET status=?,error_code=?,http_status=?,finished_at=? "
                "WHERE run_id=? AND status='running'",
                (status, code, http_status, utc_now(), run_id),
            )

    def usage(self, run_id: str, attempt: int, usage: Usage | None) -> None:
        if usage is None:
            return
        with self.app.database.transaction() as db:
            row = db.execute(
                "SELECT profile_id,usage FROM chat_call_attempts WHERE run_id=? AND attempt=?",
                (run_id, attempt),
            ).fetchone()
            old = json.loads(row["usage"]) if row["usage"] is not None else None
            report = {f: getattr(usage, f) for f in USAGE_FIELDS}
            report["cost_usd"] = str(usage.cost_usd) if usage.cost_usd is not None else None
            if old:
                report = {f: report[f] if report[f] is not None else old[f] for f in USAGE_FIELDS}
                report["cost_usd"] = (
                    str(usage.cost_usd) if usage.cost_usd is not None else old.get("cost_usd")
                )
            if report == old:
                return
            db.execute(
                "UPDATE chat_call_attempts SET usage=? WHERE run_id=? AND attempt=?",
                (json.dumps(report), run_id, attempt),
            )
            self.store.append(
                run_id,
                "usage",
                {
                    "run_id": run_id,
                    "attempt": attempt,
                    "profile_id": row["profile_id"],
                    "snapshot": True,
                    "usage": report,
                },
            )

    def metadata(self, run_id: str) -> dict:
        with self.app.database.transaction() as db:
            policy = db.execute(
                "SELECT requested_profile_id,initial_profile_id,selection_reason "
                "FROM chat_run_profiles WHERE run_id=?",
                (run_id,),
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM chat_call_attempts WHERE run_id=? ORDER BY attempt",
                (run_id,),
            ).fetchall()
        attempts = [
            dict(r) | {"usage": json.loads(r["usage"]) if r["usage"] else None} for r in rows
        ]
        totals = {}
        for field in USAGE_FIELDS:
            values = [(a["usage"] or {}).get(field) for a in attempts]
            known = [v for v in values if isinstance(v, int)]
            totals[field] = sum(known) if values and len(known) == len(values) else None
        return (dict(policy) if policy else {}) | {"attempts": attempts, "usage": totals}
