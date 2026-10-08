from lingnexus.common.types import SourceRef
from lingnexus.history.service import HistoryService
from lingnexus.memory.episodes import Episodes
from lingnexus.memory.knowledge import Knowledge


class MemoryService:
    def __init__(self, history: HistoryService):
        self.history = history
        self.episodes = Episodes(history.database, history)
        self.save_episode = self.episodes.save
        self.read_episode = self.episodes.read
        self.find_episodes = self.episodes.find
        self.invalidate = self.episodes.invalidate
        self.knowledge = Knowledge(history.database, history)

    def read_sources(self, refs: list[SourceRef]) -> list[dict]:
        return [self.history.read(ref) for ref in refs]
