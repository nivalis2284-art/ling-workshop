"""Bounded selected-path capture. Each model unit is one complete user turn."""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.work_events.service import scope_snapshot

from .calendar import as_utc

PAGE = 64
MAX_SOURCES = 512
MAX_NODES = 100_000


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main_conversation(app, agent_id):
    continuity = getattr(app, "continuity", None)
    if continuity is None:
        return None
    binding = continuity.main.binding(agent_id)
    return app.conversations.read(binding["conversation_id"]) if binding else None


def token(app, agent_id):
    # Indexed versions only. Ordinary append preserves the frozen prefix.
    with contextlib.closing(app.database.connect()) as db:
        binding = db.execute(
            "SELECT conversation_id,revision FROM continuity_main_conversations WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        if binding is None:
            raise Conflict("尚未绑定主干。")
        epoch = db.execute(
            "SELECT epoch FROM continuity_source_versions WHERE conversation_id=?", (binding[0],)
        ).fetchone()
        coverage = db.execute(
            "SELECT epoch FROM reflection_source_epochs WHERE agent_id=?", (agent_id,)
        ).fetchone()
    return {
        "conversation_id": binding[0],
        "binding_revision": binding[1],
        "epoch": epoch[0] if epoch else 0,
        "coverage": coverage[0] if coverage else 0,
        "grants": scope_snapshot(app, agent_id),
    }


def _pages(app, cid, head):
    seen = set()
    while head:
        batch = []
        # A read connection (not Database.transaction's BEGIN IMMEDIATE/RLock).
        # Only metadata is loaded, at most PAGE nodes per short snapshot.
        with contextlib.closing(app.database.connect()) as db:
            db.execute("BEGIN")
            for _ in range(PAGE):
                if not head:
                    break
                if head in seen or len(seen) >= MAX_NODES:
                    raise Conflict("主干来源循环或扫描超过节点上限。")
                seen.add(head)
                row = db.execute(
                    "SELECT id,parent_id,role,created_at,length(cast(text AS BLOB)) AS byte_count FROM conversation_messages WHERE id=? AND conversation_id=?",
                    (head, cid),
                ).fetchone()
                if row is None:
                    raise Conflict("主干来源缺失。")
                batch.append(dict(row))
                head = row["parent_id"]
            db.commit()
        yield batch


def _excluded(db, cid, ids):
    for mid in ids:
        # Any prior publication, including revoked coverage, is a cooldown:
        # revocation must never automatically charge again.
        if db.execute(
            "SELECT 1 FROM continuity_experience_sources s JOIN continuity_experiences e ON e.id=s.experience_id WHERE e.conversation_id=? AND s.kind='message' AND json_extract(s.ref,'$.id')=? LIMIT 1",
            (cid, mid),
        ).fetchone():
            return "covered_or_revoked"
        if db.execute(
            "SELECT 1 FROM continuity_branch_returns WHERE published_message_id=? LIMIT 1", (mid,)
        ).fetchone():
            return "work_return"
        if db.execute(
            "SELECT 1 FROM work_events e,json_each(e.members) m WHERE e.conversation_id=? AND e.status='closed' AND json_extract(m.value,'$.id')=? LIMIT 1",
            (cid, mid),
        ).fetchone():
            return "legacy_event"
    return None


def _body(app, source, limit):
    kind, ref = source["kind"], source["ref"]
    with contextlib.closing(app.database.connect()) as db:
        if kind == "message":
            row = db.execute(
                "SELECT text FROM conversation_messages WHERE id=? AND length(cast(text AS BLOB))<=?",
                (ref["id"], limit),
            ).fetchone()
            if row is None:
                raise Conflict("来源改变或超过上限。")
            return row[0]
        if kind == "record":
            chunks, size = [], 0
            for row in db.execute(
                "SELECT body FROM work_event_chunks WHERE record_id=? ORDER BY ordinal",
                (ref["id"],),
            ):
                size += len(row[0])
                if size > limit:
                    raise Conflict("来源超过上限。")
                chunks.append(row[0])
            return b"".join(chunks).decode("utf-8")
        if kind == "attachment":
            row = db.execute(
                "SELECT * FROM attachments WHERE id=? AND length(cast(extraction AS BLOB))<=?",
                (ref["id"], limit),
            ).fetchone()
            if row is None:
                raise Conflict("附件提取超过上限。")
    path = app.attachments.directory / row["blob_name"]
    if path.is_symlink() or path.resolve().parent != app.attachments.directory.resolve():
        raise Conflict("附件路径失效。")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    if digest.hexdigest() != row["sha256"]:
        raise Conflict("附件校验失效。")
    return row["extraction"]


def _turn(app, cid, nodes, grants, limit):
    sources = []
    with contextlib.closing(app.database.connect()) as db:
        reason = _excluded(db, cid, [n["id"] for n in nodes])
        if reason:
            return None, reason
        for node in nodes:
            sources.append(
                {
                    "source_id": "message:" + node["id"],
                    "kind": "message",
                    "ref": {"id": node["id"]},
                    "byte_count": node["byte_count"],
                    "occurred_at": as_utc(node["created_at"]).isoformat(),
                    "revision": 1,
                    "grants": grants,
                    "coverage": "retained",
                }
            )
            for a in db.execute(
                "SELECT a.id,a.created_at,length(cast(a.extraction AS BLOB)) AS size FROM attachments a JOIN conversation_attachments l ON l.attachment_id=a.id WHERE l.message_id=? ORDER BY l.ordinal LIMIT ?",
                (node["id"], MAX_SOURCES + 1),
            ):
                sources.append(
                    {
                        "source_id": "attachment:" + a["id"],
                        "kind": "attachment",
                        "ref": {"id": a["id"], "message_id": node["id"]},
                        "byte_count": a["size"],
                        "occurred_at": as_utc(a["created_at"]).isoformat(),
                        "revision": 1,
                        "grants": grants,
                        "coverage": "bounded",
                    }
                )
        runs = db.execute(
            "SELECT * FROM chat_runs WHERE user_message_id=? ORDER BY created_at LIMIT ?",
            (nodes[0]["id"], MAX_SOURCES + 1),
        ).fetchall()
        for run in runs:
            if run["status"] not in ("completed", "failed", "cancelled"):
                return None, "active_or_unknown"
            if db.execute(
                "SELECT 1 FROM chat_tool_effects WHERE run_id=? AND (status!='completed' OR json_extract(result,'$[1]')='unknown') LIMIT 1",
                (run["id"],),
            ).fetchone():
                return None, "active_or_unknown"
            records = db.execute(
                "SELECT * FROM work_event_records WHERE run_id=? ORDER BY step,rowid LIMIT ?",
                (run["id"], MAX_SOURCES + 1),
            ).fetchall()
            pairs = {}
            for record in records:
                if record["kind"] == "run_start":
                    continue
                if record["kind"] not in {
                    "tool_call",
                    "tool_result",
                    "guidance",
                    "guidance_applied",
                }:
                    continue
                captured = json.loads(record["grants"])
                captured.pop("captured_from", None)
                if captured != grants:
                    return None, "permissions_changed"
                if record["kind"] in {"tool_call", "tool_result"}:
                    pairs.setdefault((record["step"], record["call_id"]), set()).add(record["kind"])
                sources.append(
                    {
                        "source_id": "record:" + record["id"],
                        "kind": "record",
                        "ref": {"id": record["id"]},
                        "byte_count": record["byte_count"],
                        "occurred_at": as_utc(record["created_at"]).isoformat(),
                        "revision": 1,
                        "grants": grants,
                        "coverage": (json.loads(record["metadata"] or "{}")).get(
                            "coverage", "retained"
                        ),
                        "expected_sha": record["sha256"],
                    }
                )
            if any(p != {"tool_call", "tool_result"} for p in pairs.values()):
                return None, "incomplete_tool_exchange"
            if (
                db.execute(
                    "SELECT 1 FROM chat_tool_effects WHERE run_id=? LIMIT 1", (run["id"],)
                ).fetchone()
                and not pairs
            ):
                return None, "capture_gap"
    if len(sources) > MAX_SOURCES or sum(s["byte_count"] for s in sources) > limit:
        return None, "oversize_unit"
    size = 0
    for source in sources:
        body = _body(app, source, limit - size)
        source["byte_count"] = len(body.encode("utf-8"))
        source["sha256"] = sha(body)
        if source.get("expected_sha", source["sha256"]) != source["sha256"]:
            raise Conflict("安全记录digest改变。")
        source["text"] = body
        size += source["byte_count"]
        if size > limit:
            return None, "oversize_unit"
    stamps = [s["occurred_at"] for s in sources]
    return {
        "source_id": "turn:" + nodes[0]["id"],
        "kind": "turn",
        "sources": sources,
        "text": encode(
            [{"id": s["source_id"], "kind": s["kind"], "text": s["text"]} for s in sources]
        ),
        "byte_count": size,
        "occurred_at": min(stamps),
        "occurred_end": max(stamps),
        "coverage": "retained",
    }, None


def scan(app, agent_id, *, cutoff: datetime, max_source_bytes: int):
    conversation = main_conversation(app, agent_id)
    empty = {
        "conversation": conversation,
        "units": [],
        "bytes": 0,
        "deferred": [],
        "eligible_units": 0,
    }
    if conversation is None:
        return empty
    before = token(app, agent_id)
    cid = conversation["id"]
    chosen, deferred, pending = [], [], []
    size, island, eligible, source_count = 0, 0, 0, 0
    for page in _pages(app, cid, conversation["head_id"]):
        for node in page:
            pending.append(node)
            if node["role"] != "user":
                continue
            ordered = list(reversed(pending))
            pending = []
            if len(ordered) == 1 or any(as_utc(n["created_at"]) > as_utc(cutoff) for n in ordered):
                island += 1
                continue
            eligible += 1
            if size >= max_source_bytes or source_count >= MAX_SOURCES:
                break
            unit, reason = _turn(app, cid, ordered, before["grants"], max_source_bytes - size)
            if unit is not None and as_utc(unit["occurred_end"]) > as_utc(cutoff):
                unit, reason = None, "young_component"
            if unit is None:
                if len(deferred) < PAGE:
                    deferred.append({"source_id": ordered[0]["id"], "reason": reason})
                island += 1
                continue
            if source_count + len(unit["sources"]) > MAX_SOURCES:
                island += 1
                continue
            unit["island"] = island
            chosen.append(unit)
            size += unit["byte_count"]
            source_count += len(unit["sources"])
        else:
            continue
        break
    chosen.reverse()
    for i, unit in enumerate(chosen):
        unit["ordinal"] = i
        for source in unit["sources"]:
            source["atomic_unit_id"] = unit["source_id"]
            source["island"] = unit["island"]
    if token(app, agent_id) != before:
        raise Conflict("捕获期间来源改变，请重试。")
    return {
        "conversation": conversation,
        "units": chosen,
        "bytes": size,
        "deferred": deferred,
        "eligible_units": eligible,
        "token": before,
    }


def verify_snapshot(app, agent_id, snapshot_token):
    try:
        return None if token(app, agent_id) == snapshot_token else "sources_changed"
    except (Conflict, NotFound):
        return "sources_changed"
