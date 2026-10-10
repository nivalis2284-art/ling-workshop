"""Composition root: connects modules through their public services."""

from pathlib import Path

from lingnexus.actions.service import Actions
from lingnexus.activities import ActivityService
from lingnexus.browser import BrowserService
from lingnexus.configuration.credential_cache import CredentialCache
from lingnexus.configuration.instructions import OwnerInstructions
from lingnexus.configuration.profiles import Profiles
from lingnexus.content.service import Attachments
from lingnexus.context.builder import ContextBuilder
from lingnexus.conversations.segments import Segments
from lingnexus.conversations.service import Conversations
from lingnexus.dependencies import DependencyService
from lingnexus.development.service import DevelopmentService
from lingnexus.history.service import HistoryService
from lingnexus.identity.service import IdentityService
from lingnexus.maintenance.automatic import AutomaticBackups
from lingnexus.maintenance.service import MaintenanceService
from lingnexus.memory.service import MemoryService
from lingnexus.permissions.service import Permissions
from lingnexus.skills.service import Skills
from lingnexus.storage.db import Database


class Application:
    def __init__(self, path: Path, *, credential_cache: bool = False):
        self.database = Database(path)
        self.history = HistoryService(self.database)
        self.identity = IdentityService(self.database, self.history)
        self.memory = MemoryService(self.history)
        self.development = DevelopmentService(self.database, self.history)
        self.instructions = OwnerInstructions(self.database)
        self.context = ContextBuilder(self.database, self.identity, self.history, self.instructions)
        self.maintenance = MaintenanceService(self.database)
        self.profiles = Profiles(
            self.database,
            cache=CredentialCache(self.database.path.parent / "cache/model-credentials.bin")
            if credential_cache
            else None,
        )
        self.attachments = Attachments(self.database)
        self.conversations = Conversations(self.database, self.history)
        self.segments = Segments(self)
        self.permissions = Permissions(self.database)
        self.actions = Actions(self)
        self.skills = Skills(self.database, self.history)
        self.activities = ActivityService(self)
        self.browser: BrowserService | None = None
        self.dependencies: DependencyService | None = None
        self.automatic_backups: AutomaticBackups | None = None

    def create_agent(self, name: str) -> str:
        return self.identity.create(name)

    def close(self) -> None:
        # Release process-memory keys; the optional user-encrypted cache survives shutdown.
        self.profiles.clear_credentials()
