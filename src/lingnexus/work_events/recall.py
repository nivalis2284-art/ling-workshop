"""Read-only recall of retained work-event sources, bounded by the live budget.

Two callers share this module, and only these two:

* ``ChatToolScope.meta`` serves the ``event_records`` / ``event_read`` tools of
  one chat run. The scope — never the model — supplies the agent id and the run
  id, and therefore the conversation. The model only names an event and a source
  inside it; a call carrying ``agent_id``, ``conversation_id``, a path or SQL is
  rejected by the parameter models before dispatch. A scope that cannot prove
  that binding (the task scope) is refused by the dispatcher outright.
* ``work_events.projection`` calls :func:`read_sources` to say which events and
  pages a finished read-only turn consulted, so the next request carries a
  bounded receipt instead of replaying the text. That view is metadata only.

Budget. Before any text is produced, the calling run's private checkpoint is
loaded and the whole native payload is measured in UTF-8 bytes together with the
profile's output reserve and a packaging reserve. A page is only accepted when
the **complete serialized tool return** (the JSON the loop hands the model, with
its quoting and escaping) fits that remainder — the returned page is shrunk and
re-read until it does, and the cursor always follows the page that was actually
returned. With nothing that fits, the caller gets ``budget_exhausted`` and no
body at all. Per run at most 32 KiB of record text and 8 pages (directory or
record) are charged.

Charging and repeats. Every served page is written to ``work_event_reads`` as
metadata: the run, event revision, kind, source, cursor range, requested page
size, returned bytes and time. Original text is never copied into that table.
An identical request for the same event revision — same kind, source, cursor and
page size — is answered with an ``already_read`` reference instead of the text
again, so a repeated call can never inflate the payload: the earlier page is
still in this run's own transcript. Only ``hits`` grows; nothing is charged
twice. A different cursor, page size or event revision is a new page and is
charged normally.
"""

from __future__ import annotations

import json

from lingnexus.common.errors import Conflict, DomainError, NotFound, PermissionDenied
from lingnexus.common.types import new_id, utc_now
from lingnexus.models import ModelProfile
from lingnexus.models._sse import ProtocolError
from lingnexus.models.gateway import _request_body

RUN_BODY_LIMIT = 32_768
RUN_PAGE_LIMIT = 8
MAX_PAGE_BYTES = 16_384
MIN_PAGE_BYTES = 256
DIRECTORY_PAGE_BYTES = 8_192
PACKAGING_RESERVE = 1_024
# Every result carries a small budget block and two plain numbers besides the
# page itself. The fit reserves more than they can cost, so a page that fits
# still fits once the caller can read what is left.
RESULT_OVERHEAD_BYTES = 512
DEFAULT_WINDOW_BYTES = 32_768
DEFAULT_OUTPUT_BYTES = 4_096

_LEDGER_FIELDS = (
    "event_id",
    "event_revision",
    "kind",
    "source_id",
    "cursor",
    "next_cursor",
    "sha256",
    "byte_count",
    "page_size",
    "hits",
    "created_at",
)

_NO_BUDGET = "当前运行没有可用的上下文预算，已停止回查；请缩小问题范围或另起一轮。"
_NO_SCOPE = "该运行没有可用的对话范围，不能用回查工具。"
_UNASSEMBLED = "工作事件服务未装配，回查工具不可用。"


def read_sources(database, run_id: str) -> list[dict]:
    """The event/source/revision list one run read, as plain dicts without text.

    One entry per served page of that run, in read order: ``event_id`` and
    ``event_revision`` always, plus the source, cursor range, digest, returned
    bytes and repeat count. No original text is returned from here; the caller
    uses it to describe what was consulted, never to replay it.
    """
    if not isinstance(run_id, str) or not run_id:
        return []
    # Tolerate the composition root as the first argument; the contract is the database.
    database = getattr(database, "database", database)
    with database.transaction() as db:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_event_reads'"
        ).fetchone() is None:
            return []
        rows = db.execute(
            "SELECT " + ",".join(_LEDGER_FIELDS)
            + " FROM work_event_reads WHERE run_id=? ORDER BY created_at,rowid",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


class EventRecall:
    """One chat run's bounded, read-only view of its own conversation's events."""

    def __init__(self, app, agent_id: str, run_id: str):
        self.app = app
        self.agent_id = agent_id
        self.run_id = run_id

    # --- the two tools --------------------------------------------------

    def directory(self, args: dict) -> dict:
        """``event_records``: a metadata-only page of an event's source list."""
        event_id = args["event_id"]
        cursor = args.get("cursor") or None
        limit = args.get("limit", 20)
        # One unit of work: admission, read, re-verification and the ledger row.
        with self.app.database.transaction():
            state = self._admit("directory", event_id, "", cursor or "", limit)
            if "refusal" in state:
                return state["refusal"]
            repeat = state["repeat"]
            if repeat is not None:
                return self._reference(state, repeat, kind="directory")
            try:
                page, result, _ = self._fit_directory(state, event_id, cursor, limit)
            except DomainError as error:
                return _map_error(error)
            if "refusal" in page:
                return page["refusal"]
            if result["revision"] != state["event"]["revision"]:
                return _stale()
            try:
                spent = _envelope(_completed(result)) + RESULT_OVERHEAD_BYTES
                self._ledger(
                    state,
                    kind="directory",
                    source_id="",
                    cursor=cursor or "",
                    next_cursor=result.get("next_cursor"),
                    sha256=None,
                    byte_count=_envelope({"status": "completed", "result": result}),
                    # The key is the request the caller made, so an identical
                    # repeat of a page that had to be shrunk is still one page.
                    page_size=limit,
                    envelope_bytes=spent,
                )
            except DomainError:
                return _stale()
            self._describe(result, state, kind="directory")
            return _completed(result)

    def read(self, args: dict) -> dict:
        """``event_read``: one bounded page of a named source's original text."""
        event_id = args["event_id"]
        source_id = args["source_id"]
        cursor = args.get("cursor") or None
        max_bytes = args.get("max_bytes", 8192)
        with self.app.database.transaction():
            state = self._admit("record", event_id, source_id, cursor or "", max_bytes)
            if "refusal" in state:
                return state["refusal"]
            if state["repeat"] is not None:
                return self._reference(state, state["repeat"], kind="record")
            try:
                page, result, _ = self._fit_record(state, event_id, source_id, cursor, max_bytes)
            except DomainError as error:
                return _map_error(error)
            if "refusal" in page:
                return page["refusal"]
            if result["revision"] != state["event"]["revision"]:
                return _stale()
            try:
                spent = _envelope(_completed(result)) + RESULT_OVERHEAD_BYTES
                self._ledger(
                    state,
                    kind="record",
                    source_id=source_id,
                    cursor=cursor or "",
                    next_cursor=result.get("next_cursor"),
                    sha256=result.get("sha256"),
                    byte_count=result["page_bytes"],
                    page_size=max_bytes,
                    envelope_bytes=spent,
                )
            except DomainError:
                return _stale()
            self._describe(result, state, kind="record")
            return _completed(result)

    def _describe(self, result, state, *, kind):
        cap = state["budget"]["remaining"]
        result["window_fit_bytes"] = cap
        result["budget"] = _budget_view(self._budget(), kind=kind, cap=cap)
        # The small self-describing number converges once its digit count does.
        for _ in range(4):
            result["budget"]["page_envelope_bytes"] = _envelope(_completed(result))

    # --- admission ------------------------------------------------------

    def _admit(
        self, kind: str, event_id: str, source_id: str, cursor: str, page_size: int
    ) -> dict:
        """Everything decided before a byte of text is produced."""
        scope = self._scope()
        if scope is None:
            return {
                "refusal": _refuse("permission_denied", "run_not_active", _NO_SCOPE)
            }
        events = getattr(self.app, "work_events", None)
        if events is None:
            return {
                "refusal": _refuse("invalid_tool_request", "event_tools_unavailable", _UNASSEMBLED)
            }
        try:
            event = events.get(
                event_id, agent_id=self.agent_id, conversation_id=scope["conversation_id"]
            )
        except DomainError as error:
            return {"refusal": _map_error(error)}
        if not event.get("valid"):
            reason = event.get("reason") or "invalid"
            code = "permission_denied" if reason == "permissions_changed" else "stale_source"
            return {
                "refusal": _refuse(code, reason, "工作来源或分支已失效，不能读取旧工作资料。")
            }
        if event.get("lifecycle") != "closed":
            return {
                "refusal": _refuse(
                    "event_not_closed",
                    "open_event",
                    "工作尚未关闭，原始执行状态仍由运行器维护；结束工作后才能回查原文。",
                )
            }
        budget = self._budget()
        if budget is None:
            return {"refusal": _refuse("budget_exhausted", "no_checkpoint", _NO_BUDGET)}
        repeat = self._repeat(
            {"event": event}, kind=kind, source_id=source_id, cursor=cursor, page_size=page_size
        )
        if repeat is None:
            if budget["remaining"] < MIN_PAGE_BYTES:
                return {
                    "refusal": _refuse("budget_exhausted", "window_exhausted", _NO_BUDGET, **budget)
                }
            if budget["pages_used"] >= RUN_PAGE_LIMIT:
                return {
                    "refusal": _refuse("budget_exhausted", "page_limit_reached", _NO_BUDGET, **budget)
                }
            if kind == "record" and budget["body_used"] >= RUN_BODY_LIMIT:
                return {
                    "refusal": _refuse(
                        "budget_exhausted", "run_body_limit_reached", _NO_BUDGET, **budget
                    )
                }
        elif budget["remaining"] < MIN_PAGE_BYTES:
            # A known page is answered by reference, which still has to fit.
            return {
                "refusal": _refuse("budget_exhausted", "window_exhausted", _NO_BUDGET, **budget)
            }
        return {
            "events": events,
            "event": event,
            "budget": budget,
            "repeat": repeat,
            "conversation_id": scope["conversation_id"],
        }

    def _scope(self) -> dict | None:
        """The conversation this run actually belongs to, never a model argument.

        A finished, foreign or unbound run is not a recall scope: the run must be
        the live one, and its conversation must still have the run's own user
        message selected, exactly as the runtime frozen that input.
        """
        with self.app.database.transaction() as db:
            row = db.execute(
                "SELECT agent_id,conversation_id,user_message_id,status FROM chat_runs WHERE id=?",
                (self.run_id,),
            ).fetchone()
            if row is None or row["agent_id"] != self.agent_id or not row["conversation_id"]:
                return None
            if row["status"] != "running" or not row["user_message_id"]:
                return None
            conversation = db.execute(
                "SELECT head_id FROM conversations WHERE id=?", (row["conversation_id"],)
            ).fetchone()
        if conversation is None or conversation["head_id"] != row["user_message_id"]:
            return None
        return {"agent_id": row["agent_id"], "conversation_id": row["conversation_id"]}

    def _budget(self) -> dict | None:
        """The live window remainder, or None when no safe page can be proven."""
        with self.app.database.transaction() as db:
            stored = db.execute(
                "SELECT state,updated_at FROM chat_run_checkpoints WHERE run_id=?", (self.run_id,)
            ).fetchone()
        if stored is None:
            return None
        try:
            state = json.loads(stored["state"])
        except ValueError:
            return None
        if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
            return None
        profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
        window = profile.get("context_window_tokens") or DEFAULT_WINDOW_BYTES
        output = profile.get("max_output_tokens") or DEFAULT_OUTPUT_BYTES
        try:
            native_profile = ModelProfile.model_validate(profile)
            final_body = _request_body(native_profile, state["payload"], state.get("effort"))
        except (ValueError, ProtocolError):
            return None
        used = _size(final_body)
        with self.app.database.transaction() as db:
            rows = db.execute(
                "SELECT kind,byte_count,model_step,envelope_bytes FROM work_event_reads WHERE run_id=?",
                (self.run_id,),
            ).fetchall()
        # The runtime advances step only after all current results enter the
        # native payload. Step identity, rather than wall-clock comparison,
        # distinguishes pending results from results already inside `used`.
        pending = sum(
            row["envelope_bytes"] for row in rows if row["model_step"] >= state.get("step", 1)
        )
        return {
            "window_bytes": window,
            "payload_bytes": used,
            "output_reserve": output,
            "packaging_reserve": PACKAGING_RESERVE,
            "remaining": window - used - output - PACKAGING_RESERVE - pending,
            "body_used": sum(row["byte_count"] for row in rows if row["kind"] == "record"),
            "pages_used": len(rows),
            "model_step": state.get("step", 1),
        }

    # --- fitting one page to the real serialized size --------------------

    def _fit_directory(self, state: dict, event_id: str, cursor: str | None, limit: int) -> tuple:
        """The largest metadata page whose whole tool return still fits.

        The search is over the page size the service is asked for, exactly like a
        caller shrinking its own request, so the ``next_cursor`` that is returned
        follows the entries the model actually received.
        """
        cap = max(0, min(DIRECTORY_PAGE_BYTES, state["budget"]["remaining"] - RESULT_OVERHEAD_BYTES))
        best = None
        low, high = 1, limit
        while low <= high:
            middle = (low + high) // 2
            page = state["events"].directory(
                event_id,
                agent_id=self.agent_id,
                conversation_id=state["conversation_id"],
                cursor=cursor,
                limit=middle,
            )
            result = _directory_result(page, middle)
            if _envelope({"status": "completed", "result": result}) <= cap:
                best = (page, result)
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            return {
                "refusal": _refuse(
                    "budget_exhausted", "directory_page_too_large", _NO_BUDGET, **state["budget"]
                )
            }, {}, cap
        return (*best, cap)

    def _fit_record(
        self, state: dict, event_id: str, source_id: str, cursor: str | None, max_bytes: int
    ) -> tuple:
        """The largest text page whose whole tool return still fits the remainder."""
        budget = state["budget"]
        cap = max(0, budget["remaining"] - RESULT_OVERHEAD_BYTES)
        room = int(min(max_bytes, MAX_PAGE_BYTES, RUN_BODY_LIMIT - budget["body_used"]))
        if room < MIN_PAGE_BYTES:
            reason = (
                "run_body_limit_reached"
                if RUN_BODY_LIMIT - budget["body_used"] < MIN_PAGE_BYTES
                else "window_exhausted"
            )
            return {"refusal": _refuse("budget_exhausted", reason, _NO_BUDGET, **budget)}, {}, 0
        while True:
            page = state["events"].read_record(
                event_id,
                source_id,
                agent_id=self.agent_id,
                conversation_id=state["conversation_id"],
                cursor=cursor,
                max_bytes=room,
            )
            result = _record_result(page, room, max_bytes)
            total = _envelope({"status": "completed", "result": result})
            text_bytes = result["page_bytes"]
            if total <= cap and text_bytes + budget["body_used"] <= RUN_BODY_LIMIT:
                return page, result, cap
            # Quoting and escaping can cost more than the text itself, so shrink
            # by the measured overhead of this very page and read it again: the
            # cursor that is finally returned belongs to the page that was sent.
            escaped = len(json.dumps(page["text"], ensure_ascii=False).encode("utf-8")) - 2
            ratio = (escaped / text_bytes) if text_bytes else 1.0
            allowed = float(min(cap - (total - escaped), RUN_BODY_LIMIT - budget["body_used"]))
            smaller = int(allowed / max(ratio, 1.0)) if allowed > 0 else 0
            smaller = min(smaller, room - 1)
            if smaller < MIN_PAGE_BYTES:
                return {
                    "refusal": _refuse("budget_exhausted", "window_exhausted", _NO_BUDGET, **budget)
                }, {}, 0
            room = smaller

    # --- repeats and the ledger -----------------------------------------

    def _repeat(
        self, state: dict, *, kind: str, source_id: str, cursor: str, page_size: int
    ) -> dict | None:
        """The already-charged row for this exact request, or None.

        The request key is the run, the kind, the event revision, the source, the
        cursor and the requested page size: an identical repeat is answered from
        the same fragment and only bumps ``hits``. Any difference — a different
        cursor, page size or event revision — is a new page and is charged.
        """
        with self.app.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM work_event_reads WHERE run_id=? AND kind=? AND event_id=? "
                "AND event_revision=? AND source_id=? AND cursor=? AND page_size=?",
                (
                    self.run_id,
                    kind,
                    state["event"]["id"],
                    state["event"]["revision"],
                    source_id,
                    cursor,
                    page_size,
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def _ledger(
        self,
        state: dict,
        *,
        kind: str,
        source_id: str,
        cursor: str,
        next_cursor: str | None,
        sha256: str | None,
        byte_count: int,
        page_size: int,
        envelope_bytes: int,
    ) -> None:
        """Book one served page, in the same transaction as the page itself.

        The event version and authorization are re-read here, inside the same
        transaction as the page: a change between producing the text and booking
        it refuses the page, so a revoked or rewritten event is never returned
        and never appears in the ledger as an authorized read.
        """
        events = getattr(self.app, "work_events", None)
        current = events.get(
            state["event"]["id"],
            agent_id=self.agent_id,
            conversation_id=state["conversation_id"],
        )
        if current["revision"] != state["event"]["revision"] or not current["valid"]:
            raise Conflict("事件版本或来源授权在读取期间已改变。")
        with self.app.database.transaction() as db:
            db.execute(
                "INSERT INTO work_event_reads(id,run_id,agent_id,conversation_id,event_id,"
                "event_revision,kind,source_id,cursor,next_cursor,sha256,byte_count,page_size,"
                "hits,created_at,model_step,envelope_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    new_id(),
                    self.run_id,
                    self.agent_id,
                    state["conversation_id"],
                    state["event"]["id"],
                    state["event"]["revision"],
                    kind,
                    source_id,
                    cursor,
                    next_cursor,
                    sha256,
                    byte_count,
                    page_size,
                    utc_now(),
                    state["budget"]["model_step"],
                    envelope_bytes,
                ),
            )

    def _reference(self, state: dict, row: dict, *, kind: str) -> dict:
        """A page this run already received, by reference, with no content again.

        The earlier result is still in this run's own transcript, so returning
        the text a second time could only inflate the payload. The reference
        repeats the digest, range and continuation cursor, and only bumps the
        repeat count; it is never charged to the body or page limits again.
        """
        with self.app.database.transaction() as db:
            db.execute(
                "UPDATE work_event_reads SET hits=hits+1 WHERE run_id=? AND kind=? AND event_id=? "
                "AND event_revision=? AND source_id=? AND cursor=? AND page_size=?",
                (
                    self.run_id,
                    row["kind"],
                    state["event"]["id"],
                    state["event"]["revision"],
                    row["source_id"],
                    row["cursor"],
                    row["page_size"],
                ),
            )
        result = {
            "event_id": state["event"]["id"],
            "revision": state["event"]["revision"],
            "kind": kind,
            "source_id": row["source_id"],
            "location": row["source_id"],
            "already_read": True,
            "hits": row["hits"] + 1,
            "page_size": row["page_size"],
            "byte_count": row["byte_count"],
            "sha256": row["sha256"],
            "next_cursor": row["next_cursor"],
            "coverage": state["event"].get("coverage"),
            "citation": _citation(
                state["event"]["id"], state["event"]["revision"], row["source_id"], row["sha256"]
            ),
            "text_omitted": "identical_page_already_returned",
            "budget": _budget_view(self._budget(), kind=kind),
        }
        if kind == "record":
            result["text"] = ""
        if _envelope(_completed(result)) > state["budget"]["remaining"]:
            # Even a reference needs its bytes to fit the window that is left.
            return _refuse("budget_exhausted", "window_exhausted", _NO_BUDGET, **state["budget"])
        return _completed(result)


# --- payload shaping ----------------------------------------------------


def _completed(result: dict) -> dict:
    return {"status": "completed", "result": result}


def _directory_result(page: dict, page_size: int) -> dict:
    # Metadata only: a directory entry never carries the source's text.
    return {
        "event_id": page["event_id"],
        "revision": page["revision"],
        "items": page["items"],
        "next_cursor": page.get("next_cursor"),
        "coverage": page.get("coverage"),
        "page_size": page_size,
        "citation": "event:" + str(page["event_id"]) + "@" + str(page["revision"]),
        "already_read": False,
    }


def _record_result(page: dict, page_size: int, requested: int | None = None) -> dict:
    text = page["text"]
    return {
        "event_id": page["event_id"],
        "revision": page["revision"],
        "source_id": page["source_id"],
        "location": page.get("location"),
        "text": text,
        "page_bytes": len(text.encode("utf-8")),
        "page_size": page_size,
        "requested_max_bytes": requested if requested is not None else page_size,
        "next_cursor": page.get("next_cursor"),
        "sha256": page.get("sha256"),
        "byte_count": page.get("byte_count"),
        "coverage": page.get("coverage"),
        "citation": _citation(
            page["event_id"], page["revision"], page["source_id"], page.get("sha256")
        ),
        "already_read": False,
    }


def _citation(event_id: str, revision: int, source_id: str, sha256: str | None) -> str:
    suffix = "#" + str(sha256 or "")[:12] if sha256 else ""
    return "event:" + str(event_id) + "@" + str(revision) + "/" + str(source_id) + suffix


def _budget_view(budget: dict | None, *, kind: str, cap: int = 0, spent: int = 0) -> dict:
    """What the run may still spend, read back after this page was booked.

    ``window_bytes_remaining`` is never negative: a page that could not fit is
    refused instead of returned. Only record pages carry body bytes — a directory
    page adds metadata to the page count and nothing to the text budget.
    """
    if budget is None:
        return {}
    return {
        "window_bytes_remaining": budget["remaining"],
        "window_fit_bytes": cap,
        "page_envelope_bytes": spent,
        "body_bytes_used": budget["body_used"],
        "body_bytes_limit": RUN_BODY_LIMIT,
        "pages_used": budget["pages_used"],
        "pages_limit": RUN_PAGE_LIMIT,
        "kind": kind,
    }


def _refuse(code: str, reason: str, message: str, **extra) -> dict:
    """A refusal carries no body: only why, and the numbers behind the limit."""
    numbers = {
        key: extra[key]
        for key in ("remaining", "body_used", "pages_used", "window_bytes", "payload_bytes")
        if key in extra
    }
    result = {"reason": reason, "message": message}
    if numbers:
        result["budget"] = numbers
    return {"status": "failed", "error_code": code, "result": result}


def _stale() -> dict:
    return _refuse(
        "stale_source",
        "event_revision_changed",
        "事件版本在读取期间已改变，本次结果已丢弃；请重新打开目录后再读。",
    )


def _map_error(error: DomainError) -> dict:
    if isinstance(error, NotFound):
        code = "not_found"
    elif isinstance(error, PermissionDenied):
        code = "permission_denied"
    elif isinstance(error, Conflict):
        code = "stale_source"
    else:
        code = "invalid_tool_request"
    return {"status": "failed", "error_code": code, "result": {"message": str(error)}}


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _envelope(payload: dict) -> int:
    """The bytes the loop actually hands the model for this tool return."""
    # A tool return first becomes JSON text, then rides as a string inside the
    # native request. Count both escaping layers and a native result wrapper.
    return _size(json.dumps(payload, ensure_ascii=False)) + 256
