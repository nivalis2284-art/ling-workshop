"""The chat loop's view of one agent's environment for the shared dispatcher.

Dispatch itself lives in one place — ``lingnexus.actions.dispatch`` — shared by
every loop. This module only adapts one chat run to the environment that
dispatcher needs.

A chat run now gets a **real authorized working directory**: the first directory
the owner has granted writable, read from the existing ``Permissions`` grants —
the same directory-authorization system the file tools already use, not a new
one. ``run_command`` therefore executes for real, and it executes through the
``SandboxService`` (the default restricted-local tier), never a bare host shell.

Authorization is *not* decided here. The ``BEFORE_TOOL`` permission hook reads
the same grants and rejects anything out of scope; this module only says how to
reach the caller's environment, exactly like the task scope does. When no
directory is authorized, ``workdir`` is ``None`` and the hook honestly refuses
commands. Browser sessions are delegated to the parent ``BrowserService`` when
one is present; without it a browser call is refused rather than faked.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from pathlib import Path

from lingnexus.common.errors import PermissionDenied

_BROWSER_ACTIONS = {
    "browser_goto": "goto",
    "browser_read": "read",
    "browser_fill": "fill",
    "browser_request_click": "click",
}


class ChatToolScope:
    """Adapts one chat run to the environment the shared dispatcher needs."""

    def __init__(self, app, agent_id: str, run_id: str):
        self.app = app
        self.agent_id = agent_id
        self.run_id = run_id
        self._step = 0
        self._call_id = ""

    def bound(self, step: int, call_id: str) -> ChatToolScope:
        """A per-call copy carrying this call's deterministic effect id.

        This is environment plumbing (a stable id for later reconciliation), not
        a policy decision — every allow/deny still comes from a hook. Calls in
        one step may run concurrently, so each call gets its own copy instead of
        sharing one mutable binding.
        """
        view = ChatToolScope(self.app, self.agent_id, self.run_id)
        view._step = step
        view._call_id = call_id
        return view

    @property
    def _effect_id(self) -> str:
        return f"chat:{self.run_id}:{self._step}:{self._call_id}"

    @property
    def _writable(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in self.app.permissions.current(self.agent_id)["writable"])

    @property
    def workdir(self) -> Path | None:
        """The authorized working directory, or None when none is granted."""
        roots = self._writable
        return roots[0] if roots else None

    @property
    def command_available(self) -> bool:
        return self.workdir is not None

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in self.app.permissions.current(self.agent_id)["readable"])

    def read(self, raw: str) -> tuple[dict, str | None]:
        text = self.app.permissions.read_text(self.agent_id, Path(raw))
        return {"status": "completed", "result": {"text": text}}, None

    def write(self, raw: str, text: str, *, create: bool) -> tuple[dict, str | None]:
        if create:
            self.app.permissions.create_text(self.agent_id, Path(raw), text)
        else:
            self.app.permissions.write_text(self.agent_id, Path(raw), text)
        return (
            {"status": "completed", "result": {"path": raw, "written": True}},
            self._effect_id,
        )

    def read_path(self, path: Path) -> str:
        return self.app.permissions.read_text(self.agent_id, path)

    async def run(self, argv: list[str], purpose: str) -> tuple[dict, str | None]:
        """Execute for real, through the sandbox tier — never a bare host shell."""
        workdir = self.workdir
        if workdir is None:
            raise PermissionDenied("当前对话没有已授权的命令执行环境。")
        # The tier is chosen by the sandbox service; no engine is hard-coded here.
        runner = self.app.sandbox.executor(workdir, writable=True)
        result = asdict(await asyncio.to_thread(runner.execute, list(argv)))
        result["verification_passed"] = bool(
            purpose == "verify"
            and result.get("status") == "success"
            and result.get("exit_code") == 0
            and result.get("cleanup_confirmed")
            and not result.get("timed_out")
            and not result.get("output_truncated")
            and not result.get("error")
        )
        return result, self._effect_id

    async def browser(self, name: str, args: dict) -> tuple[dict, str | None]:
        """Delegate to the parent browser service; refuse honestly without one."""
        browser = getattr(self.app, "browser", None)
        if browser is None:
            raise PermissionDenied("当前对话没有可用的浏览器会话。")
        if name == "browser_state":
            return {"status": "completed", "result": await browser.state(self.agent_id)}, None
        from lingnexus.browser.policy import ActionInput

        expected = args.get("expected_text")
        raw = {key: value for key, value in args.items() if key != "expected_text"}
        raw.setdefault("action", _BROWSER_ACTIONS[name])
        # A deterministic, bounded request id makes the browser store idempotent
        # across steps, so a retried call can never repeat a recorded effect.
        raw["request_id"] = "chat:" + hashlib.sha256(self._effect_id.encode()).hexdigest()[:40]
        request = ActionInput.model_validate(raw)
        record = await browser.submit(self.agent_id, request)
        result = dict(record)
        if record.get("status") == "executing":
            result.update(status="unknown", error_code="effect_needs_review")
        if name == "browser_read":
            data = record.get("result") or {}
            result["verification_passed"] = bool(
                record.get("status") == "completed"
                and expected
                and expected.strip()
                and expected in data.get("text", "")
                and not data.get("truncated")
            )
            result["verification_expected_text"] = expected
        return result, record.get("id")

    def meta(self, name: str, args: dict) -> tuple[dict, str | None]:
        if name == "read_skill":
            # Knowledge only: reading a skill never changes what the tools may do.
            library = getattr(self.app, "skillpacks", None)
            if library is None:
                return {"status": "failed", "error_code": "skillpacks_unavailable"}, None
            return library.read(args["name"], args.get("resource")), None
        if name == "request_dependencies":
            from lingnexus.dependencies import DependencyService

            result = {
                "status": "completed",
                "dependency_candidate": DependencyService.propose(**args),
                "dependency_status": "pending_owner",
            }
            return result, None
        raise ValueError("Unknown meta tool")
