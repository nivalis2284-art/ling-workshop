"""Hook layer: the mechanism a loop uses to let policy impose control.

A loop advances execution; hooks impose control. Permissions, audit and every
other policy are injected through hooks and never written into the loop body —
that is the structural requirement of the C-phase contract (§2.3, §五). This
module is pure mechanism: it defines the three frozen events, the frozen result
shape, and a registry that runs handlers in registration order.

The three events are frozen by the contract:

- ``BEFORE_TOOL``     before a tool call (permission checks, out-of-scope blocks)
- ``AFTER_TOOL``      after a tool call (audit, result trace)
- ``BEFORE_TURN_END`` before a turn ends (the mount point for part 2)

``BEFORE_TURN_END`` is defined and runnable now. This round attaches no handler
to it, but a loop can already trigger it without error — so the future
long-term-memory work can hang off it without touching the loop body.

Handlers receive the call payload and return a ``HookResult`` (or ``None`` to
pass). A handler may be sync or async. ``run`` threads the payload through every
handler in order: a returned ``replacement`` rewrites the payload for the
handlers that follow. Any handler that returns ``allow=False`` stops the chain
and rejects the whole call with the first rejection reason. A handler that
raises is treated as a rejection (fail closed), never as a pass.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

_logger = logging.getLogger("lingnexus.audit")

# A payload is a plain dict so hooks never depend on a loop's internal types.
Payload = dict[str, Any]


class HookEvent(str, Enum):
    """The three frozen hook events (contract §2.3)."""

    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_TURN_END = "before_turn_end"


@dataclass(frozen=True)
class HookResult:
    """The frozen result shape a handler returns.

    ``allow`` is the decision, ``reason`` is the human-readable explanation
    shown to the user, and ``replacement`` optionally rewrites the payload
    (arguments or result) for the handlers that follow — or, on a rejection,
    carries an explanation payload for the loop to shape the tool result with.
    """

    allow: bool
    reason: str | None = None
    replacement: dict | None = None

    @classmethod
    def ok(cls, replacement: dict | None = None) -> HookResult:
        return cls(allow=True, reason=None, replacement=replacement)

    @classmethod
    def deny(cls, reason: str, replacement: dict | None = None) -> HookResult:
        return cls(allow=False, reason=reason, replacement=replacement)


Handler = Callable[[Payload], "HookResult | None | Awaitable[HookResult | None]"]


class HookRegistry:
    """Ordered, multi-handler registry for the frozen events.

    Register any number of handlers per event. ``run`` executes them in
    registration order and fails closed: the first rejection wins, and a
    handler that raises is itself a rejection.
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[tuple[str, Handler]]] = {}

    def register(self, event: HookEvent, handler: Handler) -> None:
        """Append ``handler`` for ``event``; order is registration order."""
        self._handlers.setdefault(HookEvent(event), []).append((_name_of(handler), handler))

    def handlers(self, event: HookEvent) -> list[Handler]:
        """The handlers registered for ``event``, in registration order."""
        return [handler for _, handler in self._handlers.get(HookEvent(event), ())]

    async def run(self, event: HookEvent, payload: Payload) -> HookResult:
        """Run every handler for ``event`` against ``payload``.

        Returns the first rejection if any handler rejects or raises, otherwise
        an allow whose ``replacement`` is the last rewritten payload (or ``None``
        when no handler rewrote anything). With no handlers this is a plain
        allow — so ``BEFORE_TURN_END`` is already safe to trigger.
        """
        current = dict(payload)
        replacement: dict | None = None
        for name, handler in self._handlers.get(HookEvent(event), ()):
            try:
                outcome = handler(current)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except Exception:  # noqa: BLE001 -- any handler failure must fail closed
                _logger.warning("hook %s raised on %s; rejecting by default", name, event)
                return HookResult(
                    allow=False,
                    reason=f"钩子 {name} 执行失败，已按拒绝处理。",
                    replacement=replacement,
                )
            if outcome is None:
                continue
            if not outcome.allow:
                return HookResult(
                    allow=False,
                    reason=outcome.reason or f"钩子 {name} 拒绝了本次操作。",
                    replacement=(
                        outcome.replacement if outcome.replacement is not None else replacement
                    ),
                )
            if outcome.replacement is not None:
                current = dict(outcome.replacement)
                replacement = current
        return HookResult(allow=True, reason=None, replacement=replacement)


def _name_of(handler: Handler) -> str:
    return getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None) or repr(handler)


# --- built-in hooks ----------------------------------------------------------

_SUMMARY_CHARS = 200


def summarize_arguments(arguments: Any) -> Any:
    """Bound one call's arguments so an audit record can never bloat storage."""
    if not isinstance(arguments, dict):
        return _summarize(arguments)
    return {str(key): _summarize(value) for key, value in arguments.items()}


def _summarize(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= _SUMMARY_CHARS:
            return value
        return value[:_SUMMARY_CHARS] + f"…(+{len(value) - _SUMMARY_CHARS})"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = repr(value)
    return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS] + "…"


def _duration_ms(payload: Payload, clock: Callable[[], float]) -> float | None:
    value = payload.get("duration_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    started = payload.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return round((clock() - float(started)) * 1000, 3)
    return None


async def _emit(sink: Callable[[dict], Any] | None, record: dict) -> None:
    if sink is None:
        _logger.info("tool_call %s", record)
        return
    outcome = sink(record)
    if inspect.isawaitable(outcome):
        await outcome


def audit_hook(
    sink: Callable[[dict], Any] | None = None, *, clock: Callable[[], float] = time.monotonic
) -> Handler:
    """Build the ``AFTER_TOOL`` audit handler.

    Records the name, a bounded argument summary, the status and the duration of
    every tool call through the caller's existing observation channel (``sink``),
    which the loop supplies — no new storage is created. With no sink the record
    goes to the ``lingnexus.audit`` logger. The handler only observes: it always
    passes, so audit can never block a call.
    """

    async def handler(payload: Payload) -> HookResult | None:
        record = {
            "kind": "tool_call",
            "name": payload.get("name"),
            "arguments": summarize_arguments(payload.get("arguments") or {}),
            "status": payload.get("status"),
            "duration_ms": _duration_ms(payload, clock),
        }
        await _emit(sink, record)
        return None

    return handler
