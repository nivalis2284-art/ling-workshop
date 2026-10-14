"""Real-time owner confirmation for operations with real-world side effects.

Confirmation is policy, so it lives in a ``BEFORE_TOOL`` hook — the loop body
never learns which operations need approval. The hook waits at the tool boundary
for the owner's decision, which arrives through the chat API. A call the owner
does not approve is denied with a readable reason, and that reason travels back
to the model as the ordinary tool result.

Why a wait and not a pre-authorization: the contract moves approval from a
task's *precondition* to a *real-time constraint inside the loop* (§2.6). The
gate set is deliberately small — arbitrary command execution is the operation
with unbounded real-world effects. Scoped file edits inside an authorized
directory are the Agent's ordinary work and are not gated.

Remote (MCP) tools are gated too, by name prefix. The client cannot know what a
remote tool does, so every ``mcp__*`` call is treated as side-effecting: it is
the same unbounded, prompt-injectable class as ``run_command``. Being configured
by the owner authorizes *connecting* the server, not silently carrying out each
call. This is what keeps the "no extra authority" rule true rather than nominal.

The owner can also hand one conversation a standing answer: when its access
mode is ``full`` (owned by ``lingnexus.runtime.access``), gated calls in that
conversation are released without waiting. The release travels back to the loop
as a hook-result marker so the tool record stays honest about which calls
skipped the gate; the sandbox, audit, directory grants and browser approvals
are all unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from lingnexus.common.errors import NotFound

from .hooks import Handler, HookResult

DEFAULT_GATED = frozenset({"run_command"})
MCP_TOOL_PREFIX = "mcp__"
"""Remote tools are declared ``mcp__<server>__<tool>`` (see ``lingnexus.mcp``)."""


def is_gated(name: str, gated: frozenset[str] = DEFAULT_GATED) -> bool:
    """Whether a tool needs real-time owner approval before it runs."""
    return name in gated or name.startswith(MCP_TOOL_PREFIX)
DEFAULT_TIMEOUT_SECONDS = 900.0


@dataclass
class _Pending:
    """One outstanding confirmation: what is being asked and the owner's answer."""

    summary: dict
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: bool | None = None


class ConfirmationService:
    """In-memory, per-process confirmations for the runs of one server.

    A run lives and dies inside one process, so its confirmations do too. An
    approved (run, fingerprint) stays approved for that run, so the model may
    repeat the exact same call without asking again.
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._approved: set[tuple[str, str]] = set()

    @staticmethod
    def fingerprint(name: str, arguments: dict) -> str:
        payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256((name + "\n" + payload).encode()).hexdigest()[:16]

    def pending(self, run_id: str) -> list[dict]:
        """The confirmations still awaiting an owner decision for this run."""
        return [
            {"run_id": key[0], "fingerprint": key[1], **item.summary}
            for key, item in self._pending.items()
            if key[0] == run_id
        ]

    async def wait(self, run_id: str, fingerprint: str, summary: dict) -> bool:
        """Wait for the owner's decision; False on rejection or timeout."""
        key = (run_id, fingerprint)
        if key in self._approved:
            return True
        item = self._pending.get(key)
        if item is None:
            item = _Pending(summary=summary)
            self._pending[key] = item
        try:
            await asyncio.wait_for(item.event.wait(), self.timeout_seconds)
        except TimeoutError:
            return False
        finally:
            self._pending.pop(key, None)
        return bool(item.decision)

    def decide(self, run_id: str, fingerprint: str, *, approved: bool) -> dict:
        """Record the owner's answer for one pending confirmation."""
        key = (run_id, fingerprint)
        item = self._pending.get(key)
        if item is None:
            raise NotFound("该确认请求不存在或已处理。")
        item.decision = approved
        if approved:
            self._approved.add(key)
        item.event.set()
        return {"run_id": run_id, "fingerprint": fingerprint, "approved": approved}


def confirmation_hook(
    confirmations: ConfirmationService,
    *,
    gated: frozenset[str] = DEFAULT_GATED,
    access=None,
    notifier=None,
) -> Handler:
    """Build the ``BEFORE_TOOL`` handler that enforces real-time confirmation.

    ``access`` (optional) is the per-conversation access-mode service: for a
    gated call whose conversation is in ``full`` mode the handler releases the
    call immediately — no waiting, no notification — and marks the release in
    the returned result for the loop to record. ``notifier`` (optional) is
    awaited once per newly-pending call so the owner can be told out of band;
    it must never change the decision.
    """

    async def handler(payload: dict) -> HookResult | None:
        name = payload.get("name")
        if not isinstance(name, str) or not is_gated(name, gated):
            return None
        context = payload.get("context") or {}
        run_id = context.get("run_id")
        if not run_id:
            return None
        conversation_id = context.get("conversation_id")
        if (
            access is not None
            and conversation_id
            and access.mode(conversation_id) == "full"
        ):
            # The conversation's standing answer: release without waiting.
            # The marker rides back through the result so the loop can label
            # the record; nothing else about the call changes.
            return HookResult.ok({**payload, "full_access": True})
        arguments = payload.get("arguments") or {}
        fingerprint = confirmations.fingerprint(name, arguments)
        summary = {"tool": name, "arguments": arguments}
        if notifier is not None and not confirmations.pending(run_id):
            await notifier(run_id, fingerprint, summary)
        approved = await confirmations.wait(run_id, fingerprint, summary)
        if approved:
            return None
        reason = "该操作有现实副作用，需要所有者确认后才能执行。"
        return HookResult.deny(
            reason,
            {
                "blocked": True,
                "tool": name,
                "reason": reason,
                "needs_confirmation": fingerprint,
            },
        )

    return handler
