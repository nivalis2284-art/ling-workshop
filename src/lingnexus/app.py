"""Composition root: connects the first part's modules through their public services.

This root assembles only the independently runnable Agent system: conversations,
tools/actions, permissions, models, attachments, maintenance and backups. The
long-term part (history, identity, memory, development, segments, skills,
activities) is not assembled here and must not be imported by this part.
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
from lingnexus.permissions.service import Permissions
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
