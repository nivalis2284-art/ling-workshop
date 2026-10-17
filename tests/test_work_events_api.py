"""Owner-facing HTTP boundary for work events: auth, lifecycle, revocation.

These cases drive the real composition root (FastAPI test transport plus the
lifespan-owned services) and a MockTransport-backed chat runtime. They cover
what the owner can do through the local API:

* every route refuses an unauthenticated caller and a foreign origin;
* a finished chat turn can be indexed, closed, folded and re-summarised, and the
  directory pages through its sources with an opaque cursor;
* a tampered or foreign cursor is refused instead of silently continuing;
* source-scoped reads refuse a different conversation or agent;
* withdrawing the authorization that admitted the sources makes the summary,
  the fold state and the original text unavailable;
* mutating the selected branch cancels the turn that is currently admitted.

No real model, container or owner data is involved.
"""

import asyncio
import json
import threading

import httpx
import pytest

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.runtime import ChatRequest, ChatRuntime

# --- shared builders -------------------------------------------------------


def _sse(*events):
    body = "".join("data: " + json.dumps(item) + "\n\n" for item in events)
    return body + "data: [DONE]\n\n"


def _reply(text="完成"):
    return _sse(
        {
            "id": "native-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def _configure(services):
    item = services.profiles.save(
        "work-event-fixture",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="fixture",
            base_url="https://fixture.test",
        ),
        api_key="work-event-secret",
    )
    services.profiles.select(item["id"], None)
    return item["id"]


def _install_runtime(client, services, handler):
    """Swap the lifespan runtime for one whose transport is fully offline."""

    def build():
        runtime = ChatRuntime(
            services, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        client.app.state.chat_runtime = runtime
        return runtime

    return client.portal.call(build)


def _finish_turn(client, runtime, agent_id, conversation_id, request_id, message="把导出文件命名改好"):
    run = client.portal.call(
        runtime.submit,
        ChatRequest(
            agent_id=agent_id,
            message=message,
            request_id=request_id,
            conversation_id=conversation_id,
        ),
    )
    for _ in range(400):
        status = client.portal.call(runtime.get_run, run["id"])
        if status["status"] != "running":
            return status
        client.portal.call(asyncio.sleep, 0.05)
    raise AssertionError("the turn never reached a terminal state")


def _owner_setup(client, headers, name="灵"):
    agent_id = client.post("/agents", headers=headers, json={"name": name}).json()["id"]
    conversation_id = client.post(
        "/conversations", headers=headers, json={"agent_id": agent_id, "title": "工作"}
    ).json()["id"]
    return agent_id, conversation_id


def _closed(client, headers, agent_id, conversation_id, runtime, request_id="api-turn"):
    """A closed, retained work event containing one completed chat turn.

    The boundary is opened first so the turn's user message falls inside the
    span; closing then indexes the sources of that finished run.
    """
    begun = client.post(
        f"/conversations/{conversation_id}/work-events",
        headers=headers,
        json={"title": "导出文件命名", "request_id": f"event-{request_id}"},
    )
    assert begun.status_code == 201, begun.text
    event_id = begun.json()["id"]
    _finish_turn(client, runtime, agent_id, conversation_id, request_id)
    closed = client.post(
        f"/work-events/{event_id}/close",
        headers=headers,
        json={"expected_revision": begun.json()["revision"]},
    )
    assert closed.status_code == 200, closed.text
    return event_id, closed.json()


def _plain_replies():
    return lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=_reply()
    )


# --- owner authentication -------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/conversations/{cid}/work-events", None),
        ("post", "/conversations/{cid}/work-events", {"title": "越权", "request_id": "x"}),
        ("get", "/work-events/{eid}", None),
        ("post", "/work-events/{eid}/close", {"expected_revision": 1}),
        ("post", "/work-events/{eid}/fold", {"expected_revision": 1, "folded": True}),
        ("post", "/work-events/{eid}/summary", {"expected_revision": 1, "text": "越权"}),
        ("get", "/work-events/{eid}/records", None),
        ("get", "/work-events/{eid}/records/message:1", None),
        ("get", "/agents/{aid}/work-event-settings", None),
        ("get", "/work-events/{eid}/summary-jobs", None),
    ],
)
def test_work_event_routes_refuse_an_unauthenticated_caller(client, method, path, body):
    target = path.format(cid="missing", eid="missing", aid="missing")
    call = getattr(client, method)
    response = call(target, json=body) if body is not None else call(target)
    assert response.status_code == 401


def test_work_event_routes_reject_a_foreign_origin(client, headers):
    response = client.get(
        "/conversations/missing/work-events",
        headers=dict(headers, Origin="http://evil.example"),
    )
    assert response.status_code == 403


# --- lifecycle, folding and summary ---------------------------------------


def test_begin_close_fold_and_summary_round_trip(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        event_id, closed = _closed(client, headers, agent_id, conversation_id, runtime)

        assert closed["status"] == "closed"
        assert closed["coverage"] == "retained"
        assert closed["source_count"] > 0
        assert closed["summary"].startswith("工作目标：导出文件命名")
        assert "执行已结束" in closed["summary"]
        # A completed turn with no tool verification must not claim success.
        assert "结果未全部核验" in closed["summary"]
        # Closing a fully retained, valid event folds its input automatically.
        assert closed["folded"] is True

        listed = client.get(f"/conversations/{conversation_id}/work-events", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [event_id]

        unfolded = client.post(
            f"/work-events/{event_id}/fold",
            headers=headers,
            json={"expected_revision": closed["revision"], "folded": False},
        )
        assert unfolded.status_code == 200, unfolded.text
        assert unfolded.json()["folded"] is False
        assert unfolded.json()["revision"] == closed["revision"] + 1

        folded = client.post(
            f"/work-events/{event_id}/fold",
            headers=headers,
            json={"expected_revision": unfolded.json()["revision"], "folded": True},
        )
        assert folded.status_code == 200, folded.text
        assert folded.json()["folded"] is True
        assert folded.json()["revision"] == closed["revision"] + 2

        edited = client.post(
            f"/work-events/{event_id}/summary",
            headers=headers,
            json={"expected_revision": folded.json()["revision"], "text": "所有者改写的概况。"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["summary"] == "所有者改写的概况。"
        assert edited.json()["summary_kind"] == "owner"

        # The previous summary revision stays in the version history.
        with services.database.transaction() as db:
            kinds = [
                row[0]
                for row in db.execute(
                    "SELECT kind FROM work_event_summaries WHERE event_id=? ORDER BY revision",
                    (event_id,),
                )
            ]
        assert kinds == ["rule", "owner"]
    finally:
        client.portal.call(runtime.aclose)


def test_close_refuses_a_stale_revision_and_an_empty_work_span(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        begun = client.post(
            f"/conversations/{conversation_id}/work-events",
            headers=headers,
            json={"title": "空工作", "request_id": "empty-span"},
        )
        assert begun.status_code == 201, begun.text
        event_id = begun.json()["id"]

        # No finished run is inside the span yet, so there is nothing to close.
        refused = client.post(
            f"/work-events/{event_id}/close", headers=headers, json={"expected_revision": 1}
        )
        assert refused.status_code == 409

        _finish_turn(client, runtime, agent_id, conversation_id, "empty-span-turn")
        closed = client.post(
            f"/work-events/{event_id}/close", headers=headers, json={"expected_revision": 1}
        )
        assert closed.status_code == 200, closed.text

        # The event moved on: the same revision may not be applied twice.
        assert client.post(
            f"/work-events/{event_id}/fold",
            headers=headers,
            json={"expected_revision": 1, "folded": True},
        ).status_code == 409
        assert client.post(
            f"/work-events/{event_id}/summary",
            headers=headers,
            json={"expected_revision": 1, "text": "过期写入"},
        ).status_code == 409
    finally:
        client.portal.call(runtime.aclose)


def test_begin_is_idempotent_and_refuses_conflicting_reuse(client, headers):
    _agent, conversation_id = _owner_setup(client, headers)
    payload = {"title": "命名", "request_id": "begin-once"}
    first = client.post(
        f"/conversations/{conversation_id}/work-events", headers=headers, json=payload
    )
    assert first.status_code == 201, first.text
    again = client.post(
        f"/conversations/{conversation_id}/work-events", headers=headers, json=payload
    )
    assert again.status_code in {200, 201}
    assert again.json()["id"] == first.json()["id"]
    conflict = client.post(
        f"/conversations/{conversation_id}/work-events",
        headers=headers,
        json={"title": "另一个标题", "request_id": "begin-once"},
    )
    assert conflict.status_code == 409


def test_summary_rejects_an_oversized_or_empty_body(client, headers):
    _agent, conversation_id = _owner_setup(client, headers)
    begun = client.post(
        f"/conversations/{conversation_id}/work-events",
        headers=headers,
        json={"title": "长度", "request_id": "summary-size"},
    )
    event_id = begun.json()["id"]
    assert client.post(
        f"/work-events/{event_id}/summary",
        headers=headers,
        json={"expected_revision": 1, "text": "x" * 2049},
    ).status_code == 422
    assert client.post(
        f"/work-events/{event_id}/summary",
        headers=headers,
        json={"expected_revision": 1, "text": ""},
    ).status_code == 422


# --- source directory and cursor discipline -------------------------------


def test_directory_pages_sources_and_returns_the_original_text(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        event_id, closed = _closed(client, headers, agent_id, conversation_id, runtime, "paging")
        total = closed["source_count"]
        assert total >= 2

        seen, cursor, pages = [], None, 0
        while True:
            params = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            page = client.get(f"/work-events/{event_id}/records", headers=headers, params=params)
            assert page.status_code == 200, page.text
            body = page.json()
            assert body["revision"] == closed["revision"]
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["id"])
            cursor = body["next_cursor"]
            pages += 1
            if cursor is None:
                break
            assert pages < 100
        assert len(seen) == total
        assert len(set(seen)) == total

        message_source = next(
            item
            for item in client.get(
                f"/work-events/{event_id}/records", headers=headers
            ).json()["items"]
            if item["kind"] == "message"
        )
        first = client.get(
            f"/work-events/{event_id}/records/{message_source['id']}",
            headers=headers,
            params={"max_bytes": 4},
        )
        assert first.status_code == 200, first.text
        assert first.json()["location"] == message_source["id"]
        assert 0 < len(first.json()["text"].encode("utf-8")) <= 4

        # Walking the rest of that source reproduces the stored text exactly.
        assembled, cursor = first.json()["text"], first.json()["next_cursor"]
        while cursor is not None:
            chunk = client.get(
                f"/work-events/{event_id}/records/{message_source['id']}",
                headers=headers,
                params={"max_bytes": 4, "cursor": cursor},
            )
            assert chunk.status_code == 200, chunk.text
            assembled += chunk.json()["text"]
            cursor = chunk.json()["next_cursor"]
        # The ledger locates the member in the authoritative store, and paging
        # reproduces that stored text byte for byte.
        with services.database.transaction() as db:
            ledger = db.execute(
                "SELECT ref FROM work_event_sources WHERE event_id=? AND source_id=?",
                (event_id, message_source["id"]),
            ).fetchone()
            assert ledger is not None
            reference = json.loads(ledger[0])
            stored = db.execute(
                "SELECT text FROM conversation_messages WHERE id=?",
                (reference["id"],),
            ).fetchone()
        assert stored is not None, "the ledger must locate a stored message"
        assert assembled == stored[0]
        branch = client.get(f"/conversations/{conversation_id}/messages", headers=headers).json()
        assert reference["id"] in {message["id"] for message in branch}
    finally:
        client.portal.call(runtime.aclose)


def test_reads_refuse_tampered_foreign_and_missing_cursors(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    other_conversation = client.post(
        "/conversations", headers=headers, json={"agent_id": agent_id, "title": "另一条"}
    ).json()["id"]
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        event_id, _ = _closed(client, headers, agent_id, conversation_id, runtime, "cursor")
        other_id, _ = _closed(
            client, headers, agent_id, other_conversation, runtime, "cursor-other"
        )
        # A real cursor of another event must not be usable here.
        foreign_cursor = client.get(
            f"/work-events/{other_id}/records", headers=headers, params={"limit": 1}
        ).json()["next_cursor"]
        assert foreign_cursor

        for cursor in ("AAAA", "not-base64!!", foreign_cursor):
            refused = client.get(
                f"/work-events/{event_id}/records", headers=headers, params={"cursor": cursor}
            )
            assert refused.status_code == 409, (cursor, refused.text)

        source = client.get(f"/work-events/{event_id}/records", headers=headers).json()["items"][0]
        assert client.get(
            f"/work-events/{event_id}/records/does-not-exist", headers=headers
        ).status_code == 404
        # A record cursor is bound to its own source, so it cannot open another.
        page = client.get(
            f"/work-events/{event_id}/records/{source['id']}",
            headers=headers,
            params={"max_bytes": 4},
        ).json()
        if page["next_cursor"]:
            assert client.get(
                f"/work-events/{event_id}/records/does-not-exist",
                headers=headers,
                params={"cursor": page["next_cursor"]},
            ).status_code == 404
    finally:
        client.portal.call(runtime.aclose)


def test_read_bounds_are_enforced_by_the_boundary(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        event_id, _ = _closed(client, headers, agent_id, conversation_id, runtime, "bounds")
        source = client.get(f"/work-events/{event_id}/records", headers=headers).json()["items"][0]
        path = f"/work-events/{event_id}/records/{source['id']}"
        assert client.get(path, headers=headers, params={"max_bytes": 3}).status_code == 422
        assert client.get(path, headers=headers, params={"max_bytes": 16385}).status_code == 422
        assert client.get(
            f"/work-events/{event_id}/records", headers=headers, params={"limit": 21}
        ).status_code == 422
        assert client.get(
            f"/work-events/{event_id}/records", headers=headers, params={"limit": 0}
        ).status_code == 422
    finally:
        client.portal.call(runtime.aclose)


def test_source_reads_refuse_another_agent_or_conversation(client, headers):
    """The model-facing read path is scope-bound, not owner-wide."""
    agent_id, conversation_id = _owner_setup(client, headers)
    stranger = client.post("/agents", headers=headers, json={"name": "别体"}).json()["id"]
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        event_id, _ = _closed(client, headers, agent_id, conversation_id, runtime, "scope")
        events = services.work_events
        source = client.get(f"/work-events/{event_id}/records", headers=headers).json()["items"][0]
        base = {"agent_id": agent_id, "conversation_id": conversation_id}
        for wrong in ({"agent_id": stranger}, {"conversation_id": "not-this-conversation"}):
            scope = dict(base, **wrong)
            with pytest.raises(NotFound):
                events.directory(event_id, **scope)
            with pytest.raises(NotFound):
                events.read_record(event_id, source["id"], **scope)
        # The correct scope still works, so the refusal is not a blank wall.
        assert events.directory(event_id, **base)["items"]
    finally:
        client.portal.call(runtime.aclose)


# --- authorization withdrawal --------------------------------------------


def test_withdrawing_authorization_disables_summary_fold_and_original_reads(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    runtime = _install_runtime(client, services, _plain_replies())
    try:
        event_id, closed = _closed(client, headers, agent_id, conversation_id, runtime, "revoke")
        source = client.get(f"/work-events/{event_id}/records", headers=headers).json()["items"][0]
        assert closed["valid"] is True

        revision = client.get(f"/agents/{agent_id}/permissions", headers=headers).json()["revision"]
        updated = client.put(
            f"/agents/{agent_id}/permissions",
            headers=headers,
            json={"readable": [], "writable": [], "expected_revision": revision},
        )
        assert updated.status_code == 200, updated.text

        stale = client.get(f"/work-events/{event_id}", headers=headers)
        assert stale.status_code == 200
        assert stale.json()["valid"] is False
        assert stale.json()["summary"] == ""
        assert stale.json()["folded"] is False
        assert stale.json()["lifecycle"] == "closed"
        assert stale.json()["status"] == "stale"

        assert client.post(
            f"/work-events/{event_id}/summary",
            headers=headers,
            json={"expected_revision": closed["revision"], "text": "撤回后不能写入。"},
        ).status_code == 409
        assert client.post(
            f"/work-events/{event_id}/fold",
            headers=headers,
            json={"expected_revision": closed["revision"], "folded": True},
        ).status_code == 409
        assert client.get(
            f"/work-events/{event_id}/records", headers=headers
        ).status_code in {403, 409}
        assert client.get(
            f"/work-events/{event_id}/records/{source['id']}", headers=headers
        ).status_code in {403, 409}
    finally:
        client.portal.call(runtime.aclose)


# --- active-run cancellation on source mutation ---------------------------


def _blocked_handler(entered, release, requests):
    """A model call that stays open until released, without blocking the loop.

    The handler is asynchronous so the test transport never occupies the event
    loop thread: the owner's mutation must be able to run and cancel this call
    while it is still waiting, exactly as it would against a slow provider.
    """

    async def handler(request):
        requests.append(json.loads(request.content))
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), 15)
        except TimeoutError:
            pass
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=_reply("迟到的答复")
        )

    return handler


def _await_terminal(client, runtime, run_id):
    for _ in range(400):
        status = client.portal.call(runtime.get_run, run_id)
        if status["status"] != "running":
            return status
        client.portal.call(asyncio.sleep, 0.05)
    raise AssertionError("the run stayed running")


def test_mutating_the_branch_cancels_the_admitted_run(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    entered, requests = threading.Event(), []
    release = client.portal.call(asyncio.Event)
    runtime = _install_runtime(
        client, services, _blocked_handler(entered, release, requests)
    )
    try:
        run = client.portal.call(
            runtime.submit,
            ChatRequest(
                agent_id=agent_id,
                message="长工作",
                request_id="inflight-mutation",
                conversation_id=conversation_id,
            ),
        )
        assert entered.wait(10), "the run never reached the model"
        messages = client.get(f"/conversations/{conversation_id}/messages", headers=headers).json()
        deleted = client.post(
            f"/conversations/{conversation_id}/delete-turns",
            headers=headers,
            json={"anchors": [messages[0]["id"]]},
        )
        assert deleted.status_code == 200, deleted.text

        status = _await_terminal(client, runtime, run["id"])
        assert status["status"] == "cancelled", status
        assert len(requests) == 1
        assert status["recoverable"] is False

        client.portal.call(release.set)
        client.portal.call(asyncio.sleep, 0.3)
        # The cancelled turn produced no follow-up request and stored no reply.
        assert len(requests) == 1
        assert client.get(
            f"/conversations/{conversation_id}/messages", headers=headers
        ).json() == []
    finally:
        client.portal.call(release.set)
        client.portal.call(runtime.aclose)


def test_changing_permissions_cancels_the_admitted_run(client, headers):
    agent_id, conversation_id = _owner_setup(client, headers)
    services = client.app.state.services
    _configure(services)
    entered, requests = threading.Event(), []
    release = client.portal.call(asyncio.Event)
    runtime = _install_runtime(
        client, services, _blocked_handler(entered, release, requests)
    )
    try:
        run = client.portal.call(
            runtime.submit,
            ChatRequest(
                agent_id=agent_id,
                message="在授权变化前开始的工作",
                request_id="inflight-grant",
                conversation_id=conversation_id,
            ),
        )
        assert entered.wait(10), "the run never reached the model"
        revision = client.get(f"/agents/{agent_id}/permissions", headers=headers).json()["revision"]
        changed = client.put(
            f"/agents/{agent_id}/permissions",
            headers=headers,
            json={"readable": [], "writable": [], "expected_revision": revision},
        )
        assert changed.status_code == 200, changed.text
        status = _await_terminal(client, runtime, run["id"])
        assert status["status"] == "cancelled", status
        assert status["recoverable"] is False

        client.portal.call(release.set)
        client.portal.call(asyncio.sleep, 0.3)
        assert len(requests) == 1
        # Replaying the revoked input is refused instead of silently re-sent.
        with pytest.raises(Conflict):
            client.portal.call(runtime.recover, run["id"])
        assert len(requests) == 1
    finally:
        client.portal.call(release.set)
        client.portal.call(runtime.aclose)
