"""Single async lifespan service; owner methods never belong in model tools."""

import asyncio
from pathlib import Path
from typing import Self

from playwright.async_api import Error as PlaywrightError

from lingnexus.browser.driver import BrowserDriver
from lingnexus.browser.policy import (
    EFFECT_ACTIONS,
    ActionInput,
    OriginGrant,
    authorize,
    check_local,
)
from lingnexus.browser.store import BrowserStore
from lingnexus.common.errors import Conflict, PermissionDenied


class BrowserService:
    def __init__(self, database, data_dir: Path, *, executable_path: Path | None = None):
        self.store = BrowserStore(database)
        self.driver = BrowserDriver(self.store, data_dir, executable_path)
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> Self:
        async with self._lock:
            if not self._started:
                self.store.recover()
                self._started = True
        return self

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *_):
        await self.aclose()

    def _require_started(self):
        if not self._started:
            raise RuntimeError("Parent lifespan must await BrowserService.start()")

    async def aclose(self) -> None:
        async with self._lock:
            await self.driver.aclose()
            self._started = False

    def current_origins(self, agent_id: str) -> dict:
        return self.store.origins(agent_id)

    async def configure_origins(
        self, agent_id: str, grants: list[OriginGrant], *, expected_revision: int
    ) -> dict:
        """Trusted owner call only; not part of submit/ActionInput."""
        self._require_started()
        grants = [OriginGrant.model_validate(g.model_dump()) for g in grants]
        if len({g.origin for g in grants}) != len(grants) or len(grants) > 100:
            raise ValueError("Specify at most 100 distinct exact origins")
        for grant in grants:
            await check_local(grant)
        async with self._lock:
            result = self.store.configure(agent_id, grants, expected_revision)
            session = self.driver.sessions.get(agent_id)
            if session:
                # Revocation also closes pages so background site code cannot keep writing.
                await self.driver.close_agent(agent_id)
            return result

    async def state(self, agent_id: str) -> dict:
        self._require_started()
        async with self._lock:
            return {
                "agent_id": agent_id,
                "origins": self.store.origins(agent_id),
                "page": await self.driver.page_state(agent_id),
            }

    def get_action(self, agent_id: str, action_id: str) -> dict:
        result = self.store.get(action_id)
        if result["agent_id"] != agent_id:
            raise PermissionDenied("Browser action belongs to another agent")
        return result

    def pending_approvals(self, agent_id: str) -> list[dict]:
        return self.store.pending(agent_id)

    async def submit(self, agent_id: str, request: ActionInput) -> dict:
        """Model-safe entry: reads/fills execute; every click is prepared for owner approval."""
        self._require_started()
        request = ActionInput.model_validate(request.model_dump())
        async with self._lock:
            old = self.store.find(agent_id, request)
            if old:
                return old  # No browser launch, authorization restoration, or effect replay.
            session = self.driver.sessions.get(agent_id)
            if session and session.manual:
                raise PermissionDenied("Owner manual login is active; automation is paused")
            origins = self.store.origins(agent_id)
            if not origins["grants"] or origins["revision"] != request.origin_revision:
                raise PermissionDenied("Browser origin grant is missing or changed")
            # ActionInput validates these conditional fields; narrow them for the driver.
            url = request.url or ""
            version = request.page_version or ""
            selector = request.selector or ""
            value = request.value or ""
            if request.action == "goto":
                await authorize(origins, url, request.origin_revision)
            else:
                await self.driver.require_page(agent_id, version, request.origin_revision)
            if request.action in EFFECT_ACTIONS:
                snapshot = await self.driver.snapshot(
                    agent_id, version, request.origin_revision, selector
                )
                snapshot["action"] = request.action
                # Action and approval commit together; never leave a click without its intent.
                with self.store.database.transaction():
                    record = self.store.prepare(agent_id, request)
                    return self.store.request_approval(record["id"], snapshot)
            record = self.store.prepare(agent_id, request)
            if not self.store.mark_executing(record["id"]):
                return self.store.get(record["id"])
            try:
                if request.action == "goto":
                    result = await self.driver.goto(agent_id, url, request.origin_revision)
                elif request.action == "read":
                    result = await self.driver.read(
                        agent_id, version, request.origin_revision, request.max_chars
                    )
                else:
                    result = await self.driver.fill(
                        agent_id, version, request.origin_revision, selector, value
                    )
            except asyncio.CancelledError:
                self.store.finish(record["id"], "unknown", error_code="interrupted_execution")
                raise
            except (PlaywrightError, OSError, Conflict, PermissionDenied, ValueError):
                if request.action == "goto":
                    await self.driver.close_agent(agent_id)
                return self.store.finish(
                    record["id"], "failed", error_code="browser_operation_denied"
                )
            return self.store.finish(record["id"], "completed", result)

    async def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        expected_intent_hash: str,
        agent_id: str | None = None,
    ) -> dict:
        """Owner approves the displayed immutable intent hash, then dispatches it once."""
        self._require_started()
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
        async with self._lock:
            approval = self.store.approval(approval_id)
            if agent_id is not None and agent_id != approval["agent_id"]:
                raise PermissionDenied("Browser approval belongs to another agent")
            if approval["intent_hash"] != expected_intent_hash:
                raise Conflict("Approval intent hash does not match the displayed request")
            record = self.store.get(approval["action_id"])
            if approval["status"] != "pending" or record["status"] != "prepared":
                return record
            if decision == "reject":
                return self.store.invalidate(record["id"], "owner_rejected", rejected=True)
            intent = approval["intent"]
            try:
                current = await self.driver.snapshot(
                    approval["agent_id"],
                    intent["page_version"],
                    intent["origin_revision"],
                    intent["selector"],
                )
                if (
                    current["session_id"] != intent["session_id"]
                    or current["snapshot_hash"] != intent["snapshot_hash"]
                ):
                    raise Conflict("Browser intent changed")
            except PermissionDenied:
                return self.store.invalidate(record["id"], "origin_changed")
            except (Conflict, ValueError, PlaywrightError):
                return self.store.invalidate(record["id"], "stale_page")
            # Commit executing + approved BEFORE any event capable of an external effect.
            if not self.store.approve(approval_id):
                return self.store.get(record["id"])
            try:
                result = await self.driver.click(approval["agent_id"], intent)
            except asyncio.CancelledError:
                self.store.finish(record["id"], "unknown", error_code="effect_needs_review")
                raise
            except (PlaywrightError, OSError, Conflict, PermissionDenied, ValueError):
                return self.store.finish(record["id"], "unknown", error_code="effect_needs_review")
            return self.store.finish(record["id"], "completed", result)

    async def open_login(
        self, agent_id: str, *, url: str, origin_revision: int, headed: bool
    ) -> dict:
        """Owner explicitly opens a headed tab using only the automation profile."""
        self._require_started()
        if headed is not True:
            raise PermissionDenied("Owner must explicitly request headed manual login")
        async with self._lock:
            for pending in self.store.pending(agent_id):
                self.store.invalidate(pending["action_id"], "browser_session_ended")
            return await self.driver.open_login(agent_id, url, origin_revision)

    async def finish_login(self, agent_id: str) -> dict:
        """Close the owner's headed session. Next goto opens the same profile headless."""
        self._require_started()
        async with self._lock:
            self.store.origins(agent_id)
            session = self.driver.sessions.get(agent_id)
            if session and not session.manual:
                raise Conflict("There is no owner login session to finish")
            await self.driver.close_agent(agent_id)
            return {"manual_login": False, "page": None}
