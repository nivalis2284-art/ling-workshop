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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from lingnexus.common.errors import NotFound

from .hooks import Handler, HookResult

DEFAULT_GATED = frozenset({"run_command"})
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
    notifier=None,
) -> Handler:
    """Build the ``BEFORE_TOOL`` handler that enforces real-time confirmation.

    ``notifier`` (optional) is awaited once per newly-pending call so the owner
    can be told out of band; it must never change the decision.
    """

    async def handler(payload: dict) -> HookResult | None:
        name = payload.get("name")
        if name not in gated:
            return None
        context = payload.get("context") or {}
        run_id = context.get("run_id")
        if not run_id:
            return None
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
