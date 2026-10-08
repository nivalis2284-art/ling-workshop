"""Synthetic sources only: no test contacts an external HTTP service."""

import asyncio
import socket
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lingnexus.activities import ActivityService
from lingnexus.common.errors import Conflict, PermissionDenied, RevisionConflict
from lingnexus.common.types import SourceRef

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
URL = "https://updates.example.test/releases"
ORIGIN = "https://updates.example.test"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def service(app):
    return ActivityService(app)


@pytest.fixture(autouse=True)
def synthetic_dns(monkeypatch):
    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def file_watch(service, app, agent_id, tmp_path, **kwargs):
    target = tmp_path / "watched.txt"
    target.write_text("baseline", encoding="utf-8")
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    activity = service.create(
        agent_id,
        kind="file_watch",
        title="Check release notes",
        target=str(target),
        due_at=NOW,
        interval_seconds=60,
        **kwargs,
    )
    return activity, target


def web_watch(service, agent_id, **kwargs):
    service.configure_web_origins(agent_id, [ORIGIN], expected_revision=0)
    return service.create(
        agent_id,
        kind="web_watch",
        title="Follow the compiler release",
        target=URL,
        due_at=NOW,
        interval_seconds=60,
        **kwargs,
    )


def test_reminder_is_quiet_until_due_and_restart_keeps_one_result(
    service,
    app,
    agent_id,
    app_factory,
):
    item = service.create(agent_id, kind="reminder", title="Submit the form", due_at=NOW)
    assert run(service.tick(NOW - timedelta(seconds=1))) is None
    assert service.notifications(agent_id) == []
    result = run(service.tick(NOW))
    assert result["outcome"] == "reminded"
    assert service.read(item["id"])["status"] == "completed"
    assert len(service.evidence(item["id"])) == 1
    assert len(service.notifications(agent_id)) == 1
    reopened = ActivityService(app_factory(app.database.path))
    assert run(reopened.tick(NOW)) is None
    assert reopened.notifications(agent_id) == service.notifications(agent_id)


def test_tick_processes_at_most_one_and_missed_intervals_coalesce(service, agent_id):
    first = service.create(
        agent_id,
        kind="reminder",
        title="Recurring",
        due_at=NOW,
        interval_seconds=60,
    )
    second = service.create(agent_id, kind="reminder", title="Second", due_at=NOW)
    later = NOW + timedelta(hours=3, seconds=17)
    run(service.tick(later))
    assert len(service.notifications(agent_id)) == 1
    assert service.read(first["id"])["next_due_at"] == (
        NOW + timedelta(hours=3, minutes=1)
    ).isoformat(timespec="microseconds")
    run(service.tick(later))
    assert service.read(second["id"])["status"] == "completed"
    assert run(service.tick(later)) is None
    assert len(service.notifications(agent_id)) == 2


@pytest.mark.parametrize("interval", [0, 1, 59, -60, True, 60.5])
def test_interval_floor(service, agent_id, interval):
    with pytest.raises(ValueError):
        service.create(
            agent_id,
            kind="reminder",
            title="Too frequent",
            due_at=NOW,
            interval_seconds=interval,
        )


def test_mutations_require_current_revision_and_persist_pause(
    service,
    app,
    agent_id,
    app_factory,
):
    item = service.create(agent_id, kind="reminder", title="Original", due_at=NOW)
    updated = service.update(item["id"], title="Edited", expected_revision=1)
    assert updated["revision"] == 2
    with pytest.raises(RevisionConflict):
        service.cancel(item["id"], expected_revision=1)
    paused = service.pause(item["id"], expected_revision=2)
    reopened = ActivityService(app_factory(app.database.path))
    assert reopened.read(item["id"])["status"] == "paused"
    assert run(reopened.tick(NOW + timedelta(days=1))) is None
    active = reopened.update(
        item["id"],
        status="active",
        due_at=NOW,
        expected_revision=paused["revision"],
    )
    cancelled = reopened.cancel(item["id"], expected_revision=active["revision"])
    with pytest.raises(Conflict):
        reopened.update(item["id"], status="active", expected_revision=cancelled["revision"])


def test_file_baseline_and_changes_have_new_observation_sources(
    service,
    app,
    agent_id,
    tmp_path,
):
    item, target = file_watch(service, app, agent_id, tmp_path)
    assert run(service.tick(NOW))["outcome"] == "baseline"
    assert service.notifications(agent_id) == []
    assert run(service.tick(NOW + timedelta(seconds=60)))["outcome"] == "unchanged"
    assert len(service.evidence(item["id"])) == 1
    target.write_text("new release", encoding="utf-8")
    assert run(service.tick(NOW + timedelta(seconds=120)))["outcome"] == "changed"
    rows = service.evidence(item["id"])
    assert len(rows) == 2
    assert rows[0]["source"] != rows[1]["source"]
    observed = app.history.read(SourceRef(**rows[1]["source"]))
    assert observed["kind"] == "observation"
    assert observed["scope"] == "real"
    assert "new release" in observed["text"]
    assert rows[1]["payload"]["provenance"]["path"] == str(target)
    assert service.notifications(agent_id)[0]["evidence_id"] == rows[1]["id"]
    assert run(service.tick(NOW + timedelta(seconds=120))) is None


def test_model_needs_exact_owner_scope_and_current_file_permissions(
    service,
    app,
    agent_id,
    tmp_path,
):
    target = tmp_path / "watched.txt"
    target.write_text("test", encoding="utf-8")
    with pytest.raises(PermissionDenied):
        service.create(agent_id, kind="file_watch", title="Denied", target=str(target))
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    with pytest.raises(PermissionDenied):
        service.create(
            agent_id,
            kind="file_watch",
            title="Model",
            target=str(target),
            origin="model",
        )
    service.create(agent_id, kind="file_watch", title="Owner", target=str(target))
    assert (
        service.create(
            agent_id,
            kind="file_watch",
            title="Model",
            target=str(target),
            origin="model",
        )["origin"]
        == "model"
    )
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("test", encoding="utf-8")
    with pytest.raises(PermissionDenied):
        service.create(
            agent_id,
            kind="file_watch",
            title="Widening",
            target=str(sibling),
            origin="model",
        )
    with pytest.raises(PermissionDenied):
        service.configure_web_origins(
            agent_id,
            [ORIGIN],
            expected_revision=0,
            origin="model",
        )


def test_revoked_file_permissions_pause_and_do_not_return_after_restart(
    service,
    app,
    agent_id,
    tmp_path,
    app_factory,
):
    item, _ = file_watch(service, app, agent_id, tmp_path)
    app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=1)
    assert run(service.tick(NOW))["outcome"] == "permission_revoked"
    assert service.read(item["id"])["status"] == "paused"
    assert service.evidence(item["id"]) == []
    reopened = ActivityService(app_factory(app.database.path))
    assert run(reopened.tick(NOW + timedelta(hours=1))) is None
    assert reopened.notifications(agent_id) == []


def test_file_limit_is_bounded_and_errors_do_not_leak_content(
    service,
    app,
    agent_id,
    tmp_path,
):
    item, target = file_watch(service, app, agent_id, tmp_path)
    target.write_bytes(b"secret" * 200_000)
    result = run(service.tick(NOW))
    assert result["error_code"] == "content_too_large"
    assert service.read(item["id"])["last_error"] == "content_too_large"
    assert service.evidence(item["id"]) == []
    assert service.notifications(agent_id) == []


def test_web_needs_origin_and_exact_owner_url_scope(service, agent_id):
    with pytest.raises(PermissionDenied):
        service.create(agent_id, kind="web_watch", title="Denied", target=URL)
    service.configure_web_origins(agent_id, [ORIGIN], expected_revision=0)
    with pytest.raises(PermissionDenied):
        service.create(agent_id, kind="web_watch", title="Model", target=URL, origin="model")
    service.create(agent_id, kind="web_watch", title="Owner", target=URL)
    service.create(agent_id, kind="web_watch", title="Model", target=URL, origin="model")
    with pytest.raises(PermissionDenied):
        service.create(
            agent_id,
            kind="web_watch",
            title="Sibling",
            target=ORIGIN + "/other",
            origin="model",
        )
    with pytest.raises(RevisionConflict):
        service.configure_web_origins(agent_id, [], expected_revision=0)


def test_web_hash_changes_retain_provenance_and_ignore_unchanged(app, agent_id):
    bodies = iter([b"baseline", b"baseline", b"updated"])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=next(bodies), headers={"ETag": '"release"'})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            svc = ActivityService(app, client)
            item = web_watch(svc, agent_id)
            for seconds in [0, 60, 120]:
                await svc.tick(NOW + timedelta(seconds=seconds))
            rows = svc.evidence(item["id"])
            assert len(rows) == 2
            provenance = rows[-1]["payload"]["provenance"]
            assert provenance["requested_url"] == URL
            assert provenance["response_url"] == URL
            assert provenance["status_code"] == 200
            assert provenance["etag"] == '"release"'
            assert provenance["connected_ip"] == "93.184.216.34"
            assert app.history.read(SourceRef(**rows[-1]["source"]))["scope"] == "external"
            assert len(svc.notifications(agent_id)) == 1
            assert requests[0].headers["host"] == "updates.example.test"
            assert requests[0].url.host == "93.184.216.34"

    run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/status",
        "http://127.0.0.1/status",
        "http://10.0.0.1/status",
        "http://169.254.169.254/latest",
        "http://[::1]/status",
        "file:///tmp/test",
        "https://user:password@updates.example.test/path",
    ],
)
def test_unsafe_web_targets_are_rejected(service, agent_id, url):
    with pytest.raises((ValueError, PermissionDenied)):
        service.create(agent_id, kind="web_watch", title="Unsafe", target=url)


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"]])
def test_dns_private_addresses_never_reach_http(app, agent_id, monkeypatch, addresses):
    calls = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses
        ],
    )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, text="bad")
            )
        ) as client:
            svc = ActivityService(app, client)
            web_watch(svc, agent_id)
            assert (await svc.tick(NOW))["error_code"] == "unsafe_address"
            assert calls == []

    run(scenario())


@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}), "redirect_blocked"),
        (httpx.Response(500, text="SECRET upstream body"), "http_status"),
        (httpx.Response(200, content=b"x" * 1_048_577), "content_too_large"),
    ],
)
def test_web_errors_are_safe_and_do_not_notify(app, agent_id, response, error):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as client:
            svc = ActivityService(app, client)
            item = web_watch(svc, agent_id)
            result = await svc.tick(NOW)
            assert result["error_code"] == error
            assert svc.read(item["id"])["last_error"] == error
            assert svc.evidence(item["id"]) == []
            assert svc.notifications(agent_id) == []

    run(scenario())


@pytest.mark.parametrize("mutation", ["pause", "cancel", "edit", "revoke_web", "revoke_file"])
def test_mutation_during_async_fetch_discards_result(app, agent_id, tmp_path, mutation):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def respond(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, text="must never be committed")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            svc = ActivityService(app, client)
            item = web_watch(svc, agent_id)
            other = ActivityService(app, client)
            task = asyncio.create_task(svc.tick(NOW))
            await entered.wait()
            assert await other.tick(NOW) is None
            if mutation == "pause":
                other.pause(item["id"], expected_revision=1)
            elif mutation == "cancel":
                other.cancel(item["id"], expected_revision=1)
            elif mutation == "edit":
                other.update(item["id"], title="Revised purpose", expected_revision=1)
            elif mutation == "revoke_web":
                other.configure_web_origins(agent_id, [], expected_revision=1)
            else:
                # Any file permission change is independent of a web watch.
                app.permissions.configure(
                    agent_id,
                    readable=[tmp_path],
                    writable=[],
                    expected_revision=0,
                )
            release.set()
            result = await task
            if mutation == "revoke_file":
                assert result["outcome"] == "baseline"
            else:
                assert result["outcome"] == "discarded"
                assert svc.evidence(item["id"]) == []
            assert svc.notifications(agent_id) == []

    run(scenario())


def test_topic_without_investigator_is_pending_without_fake_reasoning(service, agent_id):
    item = service.create(agent_id, kind="topic_review", title="Learn compiler design", due_at=NOW)
    assert run(service.tick(NOW))["outcome"] == "pending_investigation"
    assert service.read(item["id"])["last_outcome"] == "pending_investigation"
    assert service.notifications(agent_id) == []
    assert service.evidence(item["id"]) == []


def test_events_are_deduped_durable_and_trigger_injected_topic_investigator(
    app,
    agent_id,
    app_factory,
):
    calls = []

    async def investigate(activity, evidence):
        calls.append((activity, evidence))
        return {
            "changed": bool(evidence["events"]),
            "summary": "Reviewed task evidence",
            "notification": "The completed task advances your compiler goal.",
        }

    svc = ActivityService(app, investigate=investigate)
    item = svc.create(
        agent_id,
        kind="topic_review",
        title="Learn compiler design",
        due_at=NOW + timedelta(days=1),
        interval_seconds=3600,
    )
    event = svc.record_event(
        agent_id,
        dedupe_key="task:completed:123",
        kind="task_completed",
        content="Finished the parser exercise",
        now=NOW,
    )
    assert (
        svc.record_event(
            agent_id,
            dedupe_key="task:completed:123",
            kind="task_completed",
            content="Finished the parser exercise",
            now=NOW,
        )
        == event
    )
    with pytest.raises(Conflict):
        svc.record_event(
            agent_id,
            dedupe_key="task:completed:123",
            kind="task_completed",
            content="Different result",
            now=NOW,
        )
    reopened = ActivityService(app_factory(app.database.path), investigate=investigate)
    assert run(reopened.tick(NOW))["outcome"] == "changed"
    assert calls[0][0]["title"] == "Learn compiler design"
    assert calls[0][1]["events"][0]["id"] == event["id"]
    assert len(reopened.notifications(agent_id)) == 1
    assert len(reopened.evidence(item["id"])) == 1
    assert run(reopened.tick(NOW)) is None
    assert run(reopened.tick(NOW + timedelta(hours=1)))["outcome"] == "unchanged"
    assert calls[-1][1]["events"] == []
    assert len(reopened.notifications(agent_id)) == 1


def test_scope_revocation_survives_restart(service, app, agent_id, tmp_path, app_factory):
    item, target = file_watch(service, app, agent_id, tmp_path)
    scope = service.scopes(agent_id)[0]
    service.revoke_scope(scope["id"], expected_revision=scope["revision"])
    reopened = ActivityService(app_factory(app.database.path))
    assert reopened.read(item["id"])["status"] == "paused"
    with pytest.raises(PermissionDenied):
        reopened.create(
            agent_id,
            kind="file_watch",
            title="Model",
            target=str(target),
            origin="model",
        )


def test_owner_api_is_mountable_and_cannot_accept_model_origin(app, agent_id):
    from lingnexus.api.activities import router

    server = FastAPI()
    server.state.services = app
    app.activities = ActivityService(app)
    server.state.owner_token = "test-only"
    server.state.allowed_origins = set()
    server.include_router(router)
    headers = {"Authorization": "Bearer test-only"}
    with TestClient(server) as client:
        assert client.get(f"/agents/{agent_id}/activities").status_code == 401
        response = client.post(
            "/activities",
            headers=headers,
            json={
                "agent_id": agent_id,
                "kind": "reminder",
                "title": "Synthetic reminder",
                "due_at": NOW.isoformat(),
            },
        )
        assert response.status_code == 201
        item = response.json()
        assert (
            client.get(f"/activities/{item['id']}", headers=headers).json()["title"]
            == item["title"]
        )
        assert (
            client.post(
                "/activities",
                headers=headers,
                json={
                    "agent_id": agent_id,
                    "kind": "reminder",
                    "title": "Forged origin",
                    "origin": "model",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/activities/{item['id']}/pause", headers=headers, json={"expected_revision": 1}
            ).json()["status"]
            == "paused"
        )


def test_owner_scope_must_be_a_file_not_a_directory(service, app, agent_id, tmp_path):
    app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
    with pytest.raises((ValueError, PermissionDenied)):
        service.create(agent_id, kind="file_watch", title="Folder", target=str(tmp_path))
    assert service.scopes(agent_id) == []


def test_model_cannot_widen_update_or_resume_owner_pause(service, app, agent_id, tmp_path):
    item, _ = file_watch(service, app, agent_id, tmp_path)
    other = tmp_path / "other.txt"
    other.write_text("synthetic", encoding="utf-8")
    with pytest.raises(PermissionDenied):
        service.update(item["id"], target=str(other), origin="model", expected_revision=1)
    paused = service.pause(item["id"], expected_revision=1)
    with pytest.raises(PermissionDenied):
        service.update(
            item["id"],
            status="active",
            origin="model",
            expected_revision=paused["revision"],
        )


def test_event_bursts_coalesce_with_a_minute_between_reviews(app, agent_id):
    async def investigate(activity, evidence):
        text = evidence["events"][-1]["content"] if evidence["events"] else "No event"
        return {"changed": True, "summary": text}

    svc = ActivityService(app, investigate=investigate)
    item = svc.create(agent_id, kind="topic_review", title="Goal", due_at=NOW)
    svc.record_event(agent_id, dedupe_key="one", kind="task_completed", content="First", now=NOW)
    run(svc.tick(NOW))
    for seconds in range(1, 20):
        svc.record_event(
            agent_id,
            dedupe_key=f"event:{seconds}",
            kind="tool_completed",
            content=f"Update {seconds}",
            now=NOW + timedelta(seconds=seconds),
        )
        assert run(svc.tick(NOW + timedelta(seconds=seconds))) is None
    assert len(svc.notifications(agent_id)) == 1
    assert run(svc.tick(NOW + timedelta(seconds=60)))["outcome"] == "changed"
    assert len(svc.notifications(agent_id)) == 2
    assert len(svc.evidence(item["id"])) == 2


def test_streamed_http_is_bounded_without_content_length_and_closes(app, agent_id):
    class LargeStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for _ in range(20):
                yield b"x" * 65_536

        async def aclose(self):
            self.closed = True

    stream = LargeStream()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream),
            )
        ) as client:
            svc = ActivityService(app, client)
            web_watch(svc, agent_id)
            assert (await svc.tick(NOW))["error_code"] == "content_too_large"
            assert stream.closed
            assert svc.notifications(agent_id) == []

    run(scenario())


def test_compressed_http_is_rejected_before_decompression(app, agent_id):
    class EncodedStream(httpx.AsyncByteStream):
        read = False
        closed = False

        async def __aiter__(self):
            self.read = True
            yield b"intentionally not a valid gzip stream"

        async def aclose(self):
            self.closed = True

    stream = EncodedStream()

    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            svc = ActivityService(app, client)
            web_watch(svc, agent_id)
            assert (await svc.tick(NOW))["error_code"] == "unsupported_encoding"
            assert not stream.read
            assert stream.closed

    run(scenario())


def test_expired_worker_claim_is_recovered_after_restart(
    service,
    app,
    agent_id,
    app_factory,
):
    item = service.create(agent_id, kind="reminder", title="Recover", due_at=NOW)
    with app.database.transaction() as db:
        db.execute(
            "UPDATE activities SET claim_token=?,claim_until=? WHERE id=?",
            ("dead-worker", (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), item["id"]),
        )
    reopened = ActivityService(app_factory(app.database.path))
    assert run(reopened.tick(NOW))["outcome"] == "reminded"
    assert run(reopened.tick(NOW)) is None
    assert len(reopened.notifications(agent_id)) == 1


def test_cancelled_async_worker_releases_claim_and_retry_is_safe(app, agent_id):
    async def scenario():
        entered = asyncio.Event()

        async def investigate(activity, evidence):
            entered.set()
            await asyncio.Event().wait()

        svc = ActivityService(app, investigate=investigate)
        item = svc.create(agent_id, kind="topic_review", title="Pending", due_at=NOW)
        task = asyncio.create_task(svc.tick(NOW))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not svc.read(item["id"])["busy"]
        assert svc.evidence(item["id"]) == []
        assert await ActivityService(app).tick(NOW) == {
            "activity_id": item["id"],
            "outcome": "pending_investigation",
            "error_code": None,
        }

    run(scenario())


def test_file_permission_revocation_between_read_and_commit_discards_content(
    service,
    app,
    agent_id,
    tmp_path,
    monkeypatch,
):
    item, _ = file_watch(service, app, agent_id, tmp_path)
    original = app.permissions.read_text

    def revoke_after_read(agent, path):
        result = original(agent, path)
        app.permissions.configure(agent, readable=[], writable=[], expected_revision=1)
        return result

    monkeypatch.setattr(app.permissions, "read_text", revoke_after_read)
    assert run(service.tick(NOW))["outcome"] == "discarded"
    assert service.read(item["id"])["status"] == "paused"
    assert service.evidence(item["id"]) == []


@pytest.mark.parametrize(
    "decision",
    [
        {"changed": "yes", "summary": "bad"},
        {"changed": True, "summary": "x" * 4097},
        {"changed": True, "summary": "Try widening", "grant_url": URL},
    ],
)
def test_invalid_investigation_cannot_create_notifications_or_new_grants(app, agent_id, decision):
    async def investigate(activity, evidence):
        return decision

    svc = ActivityService(app, investigate=investigate)
    svc.create(agent_id, kind="topic_review", title="Goal", due_at=NOW)
    assert run(svc.tick(NOW))["error_code"] == "invalid_investigation"
    assert svc.notifications(agent_id) == []
    assert svc.web_origins(agent_id)["origins"] == []


def test_pause_during_investigation_prevents_commit(app, agent_id):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def investigate(activity, evidence):
            entered.set()
            await release.wait()
            return {"changed": True, "summary": "Do not commit this"}

        svc = ActivityService(app, investigate=investigate)
        item = svc.create(agent_id, kind="topic_review", title="Goal", due_at=NOW)
        task = asyncio.create_task(svc.tick(NOW))
        await entered.wait()
        svc.pause(item["id"], expected_revision=1)
        release.set()
        assert (await task)["outcome"] == "discarded"
        assert svc.evidence(item["id"]) == []
        assert svc.notifications(agent_id) == []

    run(scenario())


def test_investigator_receives_public_activity_and_repeated_findings_stay_quiet(app, agent_id):
    async def investigate(activity, evidence):
        assert "authorization" not in activity
        assert "claim_token" not in activity
        assert "events" not in activity
        assert evidence["untrusted_source"]
        return {"changed": True, "summary": "One concrete finding"}

    svc = ActivityService(app, investigate=investigate)
    item = svc.create(agent_id, kind="topic_review", title="Goal", due_at=NOW)
    assert run(svc.tick(NOW))["outcome"] == "changed"
    assert run(svc.tick(NOW + timedelta(seconds=60)))["outcome"] == "unchanged"
    assert len(svc.notifications(agent_id)) == 1
    assert len(svc.evidence(item["id"])) == 1


def test_callback_failure_stores_only_a_finite_error_code(app, agent_id):
    async def investigate(activity, evidence):
        raise RuntimeError("SECRET upstream error " * 10_000)

    svc = ActivityService(app, investigate=investigate)
    item = svc.create(agent_id, kind="topic_review", title="Goal", due_at=NOW)
    assert run(svc.tick(NOW))["error_code"] == "investigation_failed"
    assert svc.read(item["id"])["last_error"] == "investigation_failed"
    assert svc.notifications(agent_id) == []


def test_schedule_normalizes_offsets_and_rejects_naive_dates(service, agent_id):
    with pytest.raises(ValueError):
        service.create(agent_id, kind="reminder", title="No timezone", due_at="2026-10-09T12:00:00")
    item = service.create(
        agent_id,
        kind="reminder",
        title="Local time",
        due_at="2026-10-09T20:00:00+08:00",
    )
    assert item["next_due_at"] == NOW.isoformat(timespec="microseconds")
    assert run(service.tick(NOW))["outcome"] == "reminded"


def test_injected_client_credentials_are_not_sent_to_watch_target(app, agent_id):
    def respond(request):
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-secret" not in request.headers
        assert request.extensions["sni_hostname"] == "updates.example.test"
        return httpx.Response(200, text="synthetic")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            auth=("secret", "password"),
            cookies={"session": "secret"},
            headers={"X-Secret": "secret"},
            follow_redirects=True,
        ) as client:
            svc = ActivityService(app, client)
            web_watch(svc, agent_id)
            assert (await svc.tick(NOW))["outcome"] == "baseline"

    run(scenario())
