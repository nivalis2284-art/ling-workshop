"""Persist effect intents and verify actual results; never replay an uncertain write."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from lingnexus.actions.catalog import SCHEMAS
from lingnexus.common.errors import Conflict, DomainError, NotFound, PermissionDenied
from lingnexus.common.types import SourceRef, new_id, utc_now


class Actions:
    def __init__(self, app):
        self.app = app
        self._lock = RLock()

    @contextmanager
    def memory_reads(self):
        """Pin database-only memory reads using the normal Action lock order.

        Callers may capture source revisions before publishing tool evidence.
        Never run file operations or await network work inside this context.
        """
        with self._lock, self.app.database.transaction():
            yield

    def preview_memory(self, agent_id: str, name: str, arguments: dict) -> dict:
        """Read memory without recording an Action; use inside memory_reads()."""
        if name not in {"find_memories", "read_episode", "read_source", "find_knowledge"}:
            raise ValueError("Only database memory tools can be previewed.")
        return self._invoke(agent_id, name, SCHEMAS[name].model_validate(arguments).model_dump())

    def get(self, action_id: str) -> dict:
        with self.app.database.transaction() as db:
            row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise NotFound("行动记录不存在")
            return {
                "id": row["id"],
                "agent_id": row["agent_id"],
                "name": row["name"],
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error_code": row["error_code"],
                "source": SourceRef(id=row["source_id"], revision=row["source_revision"])
                if row["source_id"]
                else None,
            }

    def prepare(self, agent_id: str, request_id: str, name: str, arguments: dict) -> dict:
        if name not in SCHEMAS or not request_id:
            raise ValueError("未启用的工具或无效请求编号")
        validated = SCHEMAS[name].model_validate(arguments).model_dump()
        encoded = json.dumps(validated, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256((name + encoded).encode()).hexdigest()
        with self.app.database.transaction() as db:
            self.app.identity.current(agent_id)
            old = db.execute(
                "SELECT id,fingerprint FROM actions WHERE agent_id=? AND request_id=?",
                (agent_id, request_id),
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("行动请求编号已用于其他参数")
                return self.get(old["id"])
            aid = new_id()
            db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    agent_id,
                    request_id,
                    name,
                    encoded,
                    fingerprint,
                    "prepared",
                    None,
                    None,
                    None,
                    None,
                    utc_now(),
                ),
            )
            return self.get(aid)

    def mark_executing(self, action_id: str) -> bool:
        with self.app.database.transaction() as db:
            return (
                db.execute(
                    "UPDATE actions SET status='executing' WHERE id=? AND status='prepared'",
                    (action_id,),
                ).rowcount
                == 1
            )

    def execute(self, agent_id: str, request_id: str, name: str, arguments: dict) -> dict:
        with self._lock:
            intent = self.prepare(agent_id, request_id, name, arguments)
            if intent["status"] in {"completed", "failed", "unknown"}:
                return intent
            if intent["status"] == "executing" or not self.mark_executing(intent["id"]):
                with self.app.database.transaction() as db:
                    db.execute(
                        "UPDATE actions SET status='unknown',error_code='effect_needs_review' WHERE id=? AND status='executing'",
                        (intent["id"],),
                    )
                return self.get(intent["id"])
            try:
                result = self._invoke(
                    agent_id, name, SCHEMAS[name].model_validate(arguments).model_dump()
                )
            except PermissionDenied:
                return self._finish(intent, "failed", None, "permission_denied")
            except FileExistsError:
                return self._finish(intent, "failed", None, "target_exists")
            except (DomainError, ValueError):
                return self._finish(intent, "failed", None, "invalid_tool_request")
            except OSError:
                # A failed write can have partial external effects; do not silently retry.
                return self._finish(
                    intent,
                    "unknown" if name in {"create_file", "write_file"} else "failed",
                    None,
                    "file_operation_failed",
                )
            return self._finish(intent, "completed", result, None)

    def _invoke(self, agent_id: str, name: str, args: dict) -> dict:
        if name == "read_file":
            return {"text": self.app.permissions.read_text(agent_id, Path(args["path"]))}
        if name == "create_file":
            self.app.permissions.create_text(agent_id, Path(args["path"]), args["text"])
            return {"created": True, "path": args["path"]}
        if name == "write_file":
            self.app.permissions.write_text(agent_id, Path(args["path"]), args["text"])
            return {"written": True, "path": args["path"]}
        if name == "find_memories":
            return {
                "episodes": self.app.memory.find_episodes(agent_id, args["query"])
                + self.app.memory.find_episodes(agent_id, args["query"], scope="external")
            }
        if name == "find_knowledge":
            return {
                "knowledge": self.app.memory.knowledge.search(agent_id, args["query"])
                + self.app.memory.knowledge.search(agent_id, args["query"], scope="external")
            }
        if name == "read_source":
            ref = SourceRef(id=args["source_id"], revision=args["revision"])
            record = self.app.history.read(ref)
            if record["agent_id"] != agent_id or record["scope"] not in {"real", "external"}:
                raise PermissionDenied("来源不在当前个体的实际经历范围")
            start, end = args["offset"], args["offset"] + args["limit"]
            return {
                "source": ref.model_dump(),
                "text": record["text"][start:end],
                "offset": start,
                "has_more": end < len(record["text"]),
                "kind": record["kind"],
                "scope": record["scope"],
            }
        if name == "read_episode":
            item = self.app.memory.read_episode(args["episode_id"])
            if item["agent_id"] != agent_id:
                raise PermissionDenied("事件不属于当前个体")
            return item
        raise ValueError("未启用工具")

    def _finish(
        self, intent: dict, status: str, result: dict | None, error_code: str | None
    ) -> dict:
        with self.app.database.transaction() as db:
            if self.get(intent["id"])["status"] != "executing":
                return self.get(intent["id"])
            evidence = json.dumps(
                {
                    "action": intent["name"],
                    "status": status,
                    "result": result,
                    "error_code": error_code,
                },
                ensure_ascii=False,
            )
            ref = self.app.history.add(
                intent["agent_id"],
                evidence,
                kind="tool",
                scope="real",
                request_id="action:" + intent["id"],
            )
            db.execute(
                "UPDATE actions SET status=?,result=?,error_code=?,source_id=?,source_revision=? WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_code,
                    ref.id,
                    ref.revision,
                    intent["id"],
                ),
            )
            activities = getattr(self.app, "activities", None)
            if (
                activities is not None
                and status == "completed"
                and intent["name"] in {"create_file", "write_file"}
            ):
                activities.record_event(
                    intent["agent_id"],
                    dedupe_key="action:" + intent["id"],
                    kind="tool_completed",
                    content=evidence,
                )
            return self.get(intent["id"])
