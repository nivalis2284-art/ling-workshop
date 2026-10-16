"""Read-only input views: replace closed spans before native replay or budgeting."""

import hashlib
import json

from lingnexus.common.errors import DomainError

from .service import scope_snapshot

READ_TOOLS = frozenset({"event_records", "event_read"})


def project_history(app, agent_id: str, conversation_id: str, messages: list[dict]):
    events = getattr(app, "work_events", None)
    if events is None:
        return messages, []
    positions = {message["id"]: index for index, message in enumerate(messages)}
    replacements: dict[int, dict] = {}
    removed: set[str] = set()
    manifests = []
    for item in events.projection_items(conversation_id, messages):
        ids = item.get("message_ids", [])
        visible = [mid for mid in ids if mid in positions]
        revoked = item.get("reason") in {"permissions_changed", "sources_unavailable"}
        folded = item.get("folded") and item.get("valid")
        if not revoked and not folded:
            continue
        if visible and any(mid in removed for mid in visible):
            continue
        if not revoked and ids:
            indexes = [positions[mid] for mid in visible]
            if not indexes or len(visible) != len(ids) or indexes != list(range(min(indexes), max(indexes) + 1)):
                continue
        if visible:
            at = min(positions[mid] for mid in visible)
        else:
            # A standalone task has no chat transcript to remove. Its overview
            # is inserted only at an explicit selected-branch return anchor.
            anchor = item.get("end_message_id") or item.get("before_message_id")
            if ids or (anchor is not None and anchor not in positions):
                continue
            at = positions[anchor] + 1 if anchor is not None else 0
        event_id = item["event_id"]
        text = (
            f"【工作经历 {event_id}】来源或授权已失效，历史内容暂不可用于本次请求。"
            if revoked else
            f"【工作经历 {event_id}】{item['summary']}\n"
            f"这是历史概况；需要证据时先用 event_records 查看 {event_id} 的目录，再按来源用 event_read 分段回查。"
        )
        synthetic_id = f"event:{event_id}:{item['revision']}"
        if at in replacements:
            # Multiple zero-width task cards share an anchor; keep both pieces
            # in one ordinary history item without touching either source.
            replacements[at]["text"] += "\n\n" + text
            synthetic_id = replacements[at]["id"]
        else:
            replacements[at] = {"id": synthetic_id, "role": "assistant", "text": text}
        removed.update(visible)
        manifests.append({
            "event_id": event_id, "revision": item["revision"],
            "projection_id": synthetic_id, "strategy": "unavailable" if revoked else "summary",
            "replaced_messages": len(visible), "coverage": item.get("coverage"),
        })
    result = []
    for index, message in enumerate(messages):
        if index in replacements:
            result.append(replacements[index])
        if message["id"] not in removed:
            result.append(message)
    if len(messages) in replacements:
        result.append(replacements[len(messages)])
    result, receipts = _receipts(app, agent_id, conversation_id, result, removed)
    return result, [*manifests, *receipts]


def _receipts(app, agent_id, conversation_id, messages, removed):
    """A later run sees a receipt, while the original exchange remains stored."""
    with app.database.transaction() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_event_reads'").fetchone():
            return messages, []
        has_reads = db.execute("SELECT 1 FROM work_event_reads WHERE agent_id=? AND conversation_id=? LIMIT 1", (agent_id, conversation_id)).fetchone()
    # The recall module owns the metadata schema; no private payload is exposed.
    if has_reads:
        from .recall import read_sources

    result, manifests = [], []
    for message in messages:
        if message["role"] not in {"assistant", "user"} or message["id"] in removed or message["id"].startswith("event:"):
            result.append(message)
            continue
        with app.database.transaction() as db:
            row = db.execute(
                "SELECT r.id,c.state FROM chat_runs r JOIN chat_run_checkpoints c ON c.run_id=r.id "
                "WHERE (c.assistant_message_id=? OR (c.assistant_message_id IS NULL AND r.user_message_id=?)) AND r.status='completed' AND r.agent_id=? "
                "AND r.conversation_id=? ORDER BY r.finished_at DESC LIMIT 1",
                (message["id"] if message["role"] == "assistant" else None, message["id"] if message["role"] == "user" else None, agent_id, conversation_id),
            ).fetchone()
            if row is None:
                result.append(message)
                continue
            names = [json.loads(r[0]).get("name") for r in db.execute(
                "SELECT data FROM chat_events WHERE run_id=? AND event='tool_started'", (row["id"],)
            )]
            unsafe = db.execute(
                "SELECT 1 FROM chat_tool_effects WHERE run_id=? AND status!='completed' LIMIT 1", (row["id"],)
            ).fetchone()
        checkpoint = json.loads(row["state"])
        invalid = False
        for dependency in checkpoint.get("event_projection", []):
            if not dependency.get("sent"):
                continue
            refs = [dependency["event_id"]] if dependency.get("event_id") else dependency.get("event_ids", [])
            for event_id in refs:
                try:
                    source = app.work_events.get(event_id, agent_id=agent_id, conversation_id=conversation_id)
                    invalid = invalid or not source["valid"]
                except DomainError:
                    invalid = True
        if names and checkpoint.get("source_grants") != scope_snapshot(app, agent_id):
            invalid = True
        if invalid and message["role"] == "assistant":
            projection_id = "invalidated:" + row["id"]
            result.append({"id": projection_id, "role": "assistant", "text": "【历史答复】依赖的来源或授权已失效，先前派生内容暂不可用于本次请求。"})
            manifests.append({"run_id": row["id"], "strategy": "unavailable", "projection_id": projection_id})
            continue
        if not names or not set(names).issubset(READ_TOOLS) or unsafe:
            result.append(message)
            continue
        sources = read_sources(app.database, row["id"]) if has_reads else []
        if not sources:
            result.append(message)
            continue
        if message["role"] == "user":
            result.append(message)
        valid = True
        event_ids = sorted({ref["event_id"] for ref in sources})
        for ref in sources:
            try:
                event = app.work_events.get(ref["event_id"], agent_id=agent_id, conversation_id=conversation_id)
                if event.get("reason") or event.get("valid") is False or event["revision"] != ref["event_revision"]:
                    valid = False
            except DomainError:
                valid = False
        if not valid:
            text = "【历史回查】来源版本或授权已改变，先前读取的细节暂不可用于本次请求。"
        else:
            answer = message["text"] if message["role"] == "assistant" else "运行未产生最终答复；原始回查结果仍保存在运行记录中。"
            short = answer if len(answer.encode("utf-8")) <= 1024 else "原答复较长，已保存在原消息中；需要具体证据时再次按来源分段回查。"
            text = "【历史回查】曾分段读取事件 " + "、".join(event_ids) + "。\n" + short
        revision = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:16]
        projection_id = f"receipt:{row['id']}:{revision}"
        result.append({"id": projection_id, "role": "assistant", "text": text})
        manifests.append({
            "run_id": row["id"], "revision": revision, "projection_id": projection_id,
            "strategy": "read_receipt" if valid else "unavailable", "event_ids": event_ids,
        })
    return result, manifests
