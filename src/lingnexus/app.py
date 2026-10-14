"""Composition root: connects the first part's modules through their public services.

This root assembles only the independently runnable Agent system: conversations,
tools/actions, permissions, models, attachments, skill packages, maintenance and
backups. The long-term part (history, identity, memory, development, segments,
the skills *accumulation* service, activities) is not assembled here and must not
be imported by this part. Note the distinction: ``skillpacks`` (this part) loads
skill *knowledge* from folders; ``skills`` (the long-term part) accumulates and
versions skills from experience.
"""

from pathlib import Path

from lingnexus.actions.service import Actions
from lingnexus.browser import BrowserService
from lingnexus.common.types import new_id, utc_now
from lingnexus.configuration.credential_cache import CredentialCache
from lingnexus.configuration.instructions import OwnerInstructions
from lingnexus.configuration.profiles import Profiles
from lingnexus.content.service import Attachments
from lingnexus.context.builder import ContextBuilder
from lingnexus.conversations.service import Conversations
from lingnexus.dependencies import DependencyService
from lingnexus.maintenance.automatic import AutomaticBackups
from lingnexus.maintenance.service import MaintenanceService
from lingnexus.mcp import McpService
from lingnexus.permissions.hooks import AppAuthorization, permission_hook
from lingnexus.permissions.service import Permissions
from lingnexus.runtime.access import ConversationAccess
from lingnexus.runtime.confirmations import ConfirmationService, confirmation_hook
from lingnexus.runtime.hooks import HookEvent, HookRegistry
from lingnexus.runtime.store import RunStore
from lingnexus.skillpacks import SkillLibrary
from lingnexus.storage.db import Database
from lingnexus.tasks.sandbox import SandboxService


class Application:
    def __init__(self, path: Path, *, credential_cache: bool = False):
        self.database = Database(path)
        self.instructions = OwnerInstructions(self.database)
        self.context = ContextBuilder(self.database, self.instructions)
        self.maintenance = MaintenanceService(self.database)
        self.profiles = Profiles(
            self.database,
            cache=CredentialCache(self.database.path.parent / "cache/model-credentials.bin")
            if credential_cache
            else None,
        )
        self.attachments = Attachments(self.database)
        self.conversations = Conversations(self.database)
        self.permissions = Permissions(self.database)
        # Skill packages are external *knowledge*, never a permission source. The
        # folders are scanned once here; nothing is granted by loading a skill —
        # every action it describes still goes through the ordinary tools, the
        # permission hook and the sandbox. With no skills installed this is inert.
        self.skillpacks = SkillLibrary(
            (
                ("project", Path.cwd() / ".lingnexus" / "skills"),
                ("user", path.parent / "skills"),
                ("bundled", Path(__file__).parent / "skillpacks" / "builtin"),
            )
        )
        # Policy is injected through hooks, never written into a loop body. The
        # permission hook is the only authorization gate for every tool call; the
        # confirmation hook adds the real-time owner gate for side-effecting calls,
        # and the access-mode service lets the owner skip that gate per conversation.
        self.hooks = HookRegistry()
        self.hooks.register(HookEvent.BEFORE_TOOL, permission_hook(AppAuthorization(self)))
        self.confirmations = ConfirmationService()
        self.access = ConversationAccess(self.database)
        run_store = RunStore(self.database)

        async def _notify_confirmation(run_id: str, fingerprint: str, summary: dict) -> None:
            run_store.append(
                run_id,
                "notice",
                {
                    "run_id": run_id,
                    "code": "confirmation_required",
                    "fingerprint": fingerprint,
                    "tool": summary["tool"],
                    "message": "该操作有现实副作用，等待所有者确认。",
                },
            )

        self.hooks.register(
            HookEvent.BEFORE_TOOL,
            confirmation_hook(
                self.confirmations, access=self.access, notifier=_notify_confirmation
            ),
        )
        # Optional MCP servers, read from the `mcp.servers` setting on first use.
        # With nothing configured this stays completely inert: no connection, no
        # tool, no extra startup cost. A remote tool is declared through the same
        # dispatcher the chat loop reads, so it passes the same hooks below.
        self.mcp = McpService(self.database)
        self.actions = Actions(self)
        self.sandbox = SandboxService(self.database)
        self.browser: BrowserService | None = None
        self.dependencies: DependencyService | None = None
        self.automatic_backups: AutomaticBackups | None = None

    def create_agent(self, name: str) -> str:
        """Register an individual by name only; the first part needs just id + name."""
        if not name.strip():
            raise ValueError("名字不能为空")
        agent_id = new_id()
        with self.database.transaction() as db:
            db.execute("INSERT INTO agents VALUES (?,?,?)", (agent_id, name.strip(), utc_now()))
        return agent_id

    def list_agents(self) -> list[dict]:
        with self.database.transaction() as db:
            return [
                {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}
                for row in db.execute("SELECT * FROM agents ORDER BY created_at")
            ]

    def close(self) -> None:
        # Release process-memory keys; the optional user-encrypted cache survives shutdown.
        self.profiles.clear_credentials()
        # Stop any MCP server processes this process started.
        self.mcp.close()
