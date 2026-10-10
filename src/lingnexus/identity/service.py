import json

from lingnexus.common.errors import NotFound
from lingnexus.common.types import SelfSnapshot, new_id, utc_now
from lingnexus.identity.changes import Changes
from lingnexus.identity.policy import guess_kind
from lingnexus.identity.revisions import Revisions
from lingnexus.storage.db import Database


class IdentityService:
    def __init__(self, database: Database, history):
        self.database = database
        self.changes = Changes(database, history, self.current)
        self.propose = self.changes.propose
        self.decide = self.changes.decide
        self.remove = self.changes.remove
        self.list_proposals = self.changes.list
        self.revisions = Revisions(database, history, self.current)
        self.rollback = self.revisions.rollback

    def create(self, name: str) -> str:
        if not name.strip():
            raise ValueError("名字不能为空")
        with self.database.transaction() as db:
            agent_id, now = new_id(), utc_now()
            db.execute("INSERT INTO agents VALUES (?,?,?)", (agent_id, name, now))
            db.execute(
                "INSERT INTO self_revisions VALUES (?,?,?,?,?)",
                (agent_id, 0, "{}", "initialize", now),
            )
            return agent_id

    def current(self, agent_id: str) -> SelfSnapshot:
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM self_revisions WHERE agent_id=? ORDER BY revision DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise NotFound("个体不存在")
            return SelfSnapshot(
                agent_id=agent_id, revision=row["revision"], entries=json.loads(row["entries"])
            )

    def entries(self, agent_id: str) -> list[dict]:
        """Every live entry together with the category and sources that produced it."""
        with self.database.transaction() as db:
            state = self.current(agent_id)
            rows = db.execute(
                "SELECT o.entry_key, p.kind, p.id AS proposal_id, s.source_id, s.source_revision "
                "FROM self_entry_origins o "
                "JOIN proposals p ON p.id=o.proposal_id "
                "LEFT JOIN proposal_sources s ON s.proposal_id=p.id "
                "WHERE o.agent_id=?",
                (agent_id,),
            ).fetchall()
        origins: dict[str, dict] = {}
        for row in rows:
            origin = origins.setdefault(
                row["entry_key"],
                {"proposal_id": row["proposal_id"], "kind": row["kind"], "sources": []},
            )
            if row["source_id"] is not None:
                origin["sources"].append(
                    {"id": row["source_id"], "revision": row["source_revision"]}
                )
        return [
            {
                "key": key,
                "text": text,
                "kind": origins.get(key, {}).get("kind") or guess_kind(key),
                "origin": origins.get(key),
            }
            for key, text in state.entries.items()
        ]

    def list_agents(self) -> list[dict]:
        with self.database.transaction() as db:
            return [
                dict(row) | {"name": self.name(row["id"]), "initial_name": row["name"]}
                for row in db.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
            ]

    def name(self, agent_id: str) -> str:
        with self.database.transaction() as db:
            row = db.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise NotFound("个体不存在")
            state = self.current(agent_id)
            return state.entries.get("identity.name") or str(row[0])
