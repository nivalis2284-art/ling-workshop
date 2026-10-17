"""M2 P4: the optional event-summary runner, entirely offline.

The contract proven here is restraint. Nothing happens while the feature is
disabled, a well formed candidate that cites sources it really saw becomes a
stored model-generated summary, and every hostile shape - bad status, invented
source ids, an oversized overview, a credential echo, a stale revision, a
revoked grant, cancellation or disable mid-flight, an outcome the model tries
to rewrite - leaves the active summary and the authoritative event state
alone. Leases, the global call budget and partial coverage are checked through
durable rows, not by trusting the returned status.
"""

import asyncio
import json

import httpx
import pytest
from test_work_events import closed_event

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.work_events.summaries import (
    GENERATED_MARKER,
    MAX_ATTEMPTS,
    EventSummaryRunner,
)


def summary_reply(overview="修好了导出命名", ids=None, outcome=None):
    value = {"overview": overview, "source_ids": list(ids or [])}
    if outcome is not None:
        value["outcome"] = outcome
    return value


def sse(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }
    return "data: " + json.dumps(frame, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"


def client_for(replies, requests=None, hook=None):
    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        if hook is not None:
            hook(len(requests) if requests is not None else 0)
        value = replies.pop(0) if isinstance(replies, list) else replies
        if isinstance(value, int):
            return httpx.Response(value, headers={"content-type": "application/json"}, text="{}")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=value)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def runner_for(app, client, **kwargs):
    return EventSummaryRunner(app, client=client, **kwargs)


def with_profile(app):
    profile = ModelProfile(
        api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
        model="fixture",
        base_url="https://fixture.test",
        context_window_tokens=200000,
        max_output_tokens=1024,
    )
    return app.profiles.save("organizer", profile, api_key="organizer-secret")["id"]


@pytest.mark.parametrize("reason", ["small_window", "credential_in_input"])
def test_input_admission_rejects_oversize_or_current_credentials_without_http(app, agent_id, reason):
    pid = with_profile(app)
    if reason == "small_window":
        profile = ModelProfile(api_family=APIFamily.OPENAI_CHAT_COMPLETIONS, model="tiny", base_url="https://fixture.test", context_window_tokens=1200, max_output_tokens=1024)
        pid = app.profiles.save("tiny", profile, api_key="organizer-secret")["id"]
    _, event, _ = closed_event(app, agent_id, body="organizer-secret" if reason == "credential_in_input" else "原文" * 500)
    requests = []
    runner = runner_for(app, client_for([], requests))
    enable(runner, agent_id, pid)
    runner.enqueue(event["id"])
    result = run(runner)
    assert requests == []
    assert result["job"]["outcome"] == "rejected"
    assert result["job"]["error_code"] == ("summary_context_limit" if reason == "small_window" else "credential_in_input")
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def enable(runner, agent_id, profile_id, **kwargs):
    return runner.configure(agent_id, True, profile_id, **kwargs)


def run(runner):
    return asyncio.run(runner.tick())


def sources(app, agent_id, cid, event_id):
    """Real directory ids: a candidate may only cite what the runner scanned."""
    return [
        item["id"]
        for item in app.work_events.directory(
            event_id, agent_id=agent_id, conversation_id=cid
        )["items"]
    ]


def table(app, sql, args=()):
    with app.database.transaction() as db:
        return db.execute(sql, args).fetchall()


def revoke(app, agent_id):
    current = app.permissions.current(agent_id)
    app.permissions.configure(agent_id, readable=[], writable=[], expected_revision=current["revision"])


CALLS_SQL = (
    "SELECT stage,outcome,input_bytes,usage FROM event_summary_calls "
    "WHERE job_id=? ORDER BY call_index"
)


# --- disabled and rule-only paths -----------------------------------------


def test_disabled_runner_makes_no_call_and_refuses_enqueue(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    requests = []
    runner = runner_for(app, client_for([], requests))
    assert runner.settings(agent_id)["enabled"] is False
    with pytest.raises(Conflict):
        runner.enqueue(event["id"])
    assert run(runner) == {"status": "idle", "processed": 0}
    assert requests == []
    assert runner.jobs(event["id"]) == []
    before = app.work_events.get(event["id"])["summary"]
    enable(runner, agent_id, profile_id)
    # Enabling alone still calls nothing without an enqueue.
    assert run(runner) == {"status": "idle", "processed": 0}
    assert requests == []
    assert app.work_events.get(event["id"])["summary"] == before


def test_no_profile_finishes_as_rule_only_without_a_request(app, agent_id):
    _cid, event, _run = closed_event(app, agent_id)
    requests = []
    runner = runner_for(app, client_for([], requests))
    enable(runner, agent_id, None)
    assert runner.enqueue(event["id"])["status"] == "queued"
    result = run(runner)
    assert result["job"]["status"] == "done"
    assert result["job"]["outcome"] == "rule_only"
    assert result["job"]["error_code"] == "no_profile"
    assert requests == []
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


# --- the happy path --------------------------------------------------------


def test_candidate_is_validated_against_sources_and_stored_as_model_text(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id, body="原始答复")
    ids = sources(app, agent_id, cid, event["id"])
    requests = []
    replies = [sse(summary_reply("局部整理", ids[:1])), sse(summary_reply("最终概况", ids[:1]))]
    runner = runner_for(app, client_for(replies, requests))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    result = run(runner)
    assert result["status"] == "ok", result
    job = result["job"]
    assert job["status"] == "done" and job["outcome"] == "applied"
    assert job["result_revision"] == event["revision"] + 1
    stored = app.work_events.get(event["id"])
    assert stored["summary_kind"] == "model"
    assert GENERATED_MARKER in stored["summary"]
    assert "最终概况" in stored["summary"]
    assert stored["outcome"] == event["outcome"]
    assert "执行已结束" in stored["summary"]
    assert job["coverage"] == "retained"
    assert job["usage"]["calls"] >= 2
    assert job["usage"]["input_tokens"] > 0
    for request in requests:
        assert "tools" not in request
        assert request["model"] == "fixture"
    kinds = [
        row[0]
        for row in table(
            app,
            "SELECT kind FROM work_event_summaries WHERE event_id=? ORDER BY revision",
            (event["id"],),
        )
    ]
    assert kinds == ["rule", "model"]
    calls = table(app, CALLS_SQL, (job["id"],))
    assert calls[0]["stage"] == "fragment" and calls[-1]["stage"] == "final"
    assert all(row["outcome"] == "ok" for row in calls)
    assert all(row["input_bytes"] > 0 and row["usage"] for row in calls)


def test_disable_cancels_queued_jobs_and_re_enable_needs_a_new_enqueue(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    runner = runner_for(app, client_for([sse(summary_reply())]))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    runner.configure(agent_id, False, profile_id)
    jobs = runner.jobs(event["id"])
    assert [row["status"] for row in jobs] == ["cancelled"]
    assert jobs[0]["error_code"] == "disabled"
    assert run(runner) == {"status": "idle", "processed": 0}
    enable(runner, agent_id, profile_id)
    again = runner.enqueue(event["id"])
    assert again["status"] == "queued" and again["id"] != jobs[0]["id"]


# --- hostile model output --------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    (
        {"overview": "x", "source_ids": ["invented-id"]},
        {"overview": "x", "source_ids": []},
        {"overview": "", "source_ids": None},
        {"overview": "汉" * 4000, "source_ids": []},
        "not json at all",
    ),
)
def test_rejected_candidates_never_touch_the_active_summary(app, agent_id, reply):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    before = app.work_events.get(event["id"])
    runner = runner_for(app, client_for([sse(reply), sse(reply), sse(reply)]))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    result = run(runner)
    assert result["job"]["outcome"] == "rejected", result
    after = app.work_events.get(event["id"])
    assert after["summary"] == before["summary"]
    assert after["revision"] == before["revision"]
    assert after["summary_kind"] == "rule"
    row = table(
        app,
        "SELECT outcome,usage FROM event_summary_calls WHERE job_id=?",
        (result["job"]["id"],),
    )[0]
    # A refusal is still a finished call with its usage kept.
    assert row["outcome"] == "rejected" and row["usage"]


def test_only_sources_sent_to_that_call_may_be_cited(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    runner = runner_for(
        app,
        client_for(
            [sse(summary_reply("局部", ids[:1])), sse(summary_reply("最终", ["someone-else"]))]
        ),
    )
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    result = run(runner)
    assert result["job"]["outcome"] == "rejected"
    assert result["job"]["error_code"] == "unknown_source"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_unsent_source_ids_cannot_be_cited_by_the_final_call(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    runner = runner_for(
        app,
        client_for([sse(summary_reply("局部", ["s1"])), sse(summary_reply("最终", ["s1", "s3"]))]),
    )
    # Three scanned sources split into three batches; a budget of two sends only
    # the first, so s3 is scanned yet never given to any model call.
    fragments = [
        {"id": "s1", "kind": "message", "coverage": "retained", "text": "一", "truncated": False},
        {"id": "s2", "kind": "text", "coverage": "retained", "text": "二" * 5000, "truncated": True},
        {"id": "s3", "kind": "text", "coverage": "retained", "text": "三", "truncated": False},
    ]
    runner._fragments = lambda value: (fragments, True)
    enable(runner, agent_id, profile_id, max_calls=2)
    runner.enqueue(event["id"])
    result = run(runner)
    assert result["job"]["outcome"] == "rejected"
    assert result["job"]["error_code"] == "unknown_source"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_credential_echo_never_persists_or_pollutes(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    leaked = sse({"overview": "organizer-secret", "source_ids": ids[:1]})
    runner = runner_for(app, client_for([leaked, leaked, leaked, leaked]))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    result = run(runner)
    assert result["job"]["outcome"] == "rejected"
    assert result["job"]["error_code"] == "credential_echo"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"
    dumped = json.dumps(runner.jobs(event["id"]), ensure_ascii=False)
    assert "organizer-secret" not in dumped


def test_http_failures_retry_then_stop_without_polluting_rows(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    runner = runner_for(app, client_for([500] * 6), retry_backoff_seconds=0)
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    assert run(runner)["job"]["status"] == "retry"
    assert run(runner)["job"]["status"] == "retry"
    final = run(runner)["job"]
    assert final["status"] == "failed" and final["error_code"] == "http_error"
    assert final["attempts"] == MAX_ATTEMPTS
    assert run(runner) == {"status": "idle", "processed": 0}  # attempts exhausted
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"
    rows = json.dumps(
        [dict(row) for row in table(app, "SELECT * FROM event_summary_jobs")],
        ensure_ascii=False,
    )
    assert "organizer-secret" not in rows and "Traceback" not in rows


# --- cancellation, revocation, staleness and restarts ---------------------


def test_cancel_pending_and_running_jobs(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    runner = runner_for(app, client_for([sse(summary_reply())]))
    enable(runner, agent_id, profile_id)
    job = runner.enqueue(event["id"])
    cancelled = asyncio.run(runner.cancel(job["id"]))
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "cancelled_by_owner"
    assert run(runner) == {"status": "idle", "processed": 0}
    with pytest.raises(NotFound):
        asyncio.run(runner.cancel("no-such-job"))

    _cid2, event2, _run2 = closed_event(app, agent_id)
    slow = runner_for(app, client_for([sse(summary_reply())]))
    enable(slow, agent_id, profile_id)
    job2 = slow.enqueue(event2["id"])
    claimed = slow._claim()
    assert claimed["id"] == job2["id"]
    asyncio.run(slow.cancel(job2["id"]))
    outcome = asyncio.run(slow._run_job(claimed))
    assert outcome["status"] == "cancelled"
    assert outcome["error_code"] == "cancelled_by_owner"
    assert app.work_events.get(event2["id"])["summary_kind"] == "rule"


def test_cancel_interrupts_an_in_flight_request(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(30)  # never answers; cancellation must not wait for it
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = EventSummaryRunner(app, client=client)
            enable(runner, agent_id, profile_id)
            job = runner.enqueue(event["id"])
            task = asyncio.create_task(runner.tick())
            await asyncio.wait_for(started.wait(), 5)
            await runner.cancel(job["id"])
            return job, await asyncio.wait_for(task, 5)

    job, result = asyncio.run(scenario())
    assert result["job"]["id"] == job["id"]
    assert result["job"]["status"] == "cancelled"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_aclose_interrupts_a_hung_request_and_releases_runner(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(30)  # cancellation must not wait for the transport
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = EventSummaryRunner(app, client=client)
            enable(runner, agent_id, profile_id)
            job = runner.enqueue(event["id"])
            task = asyncio.create_task(runner.tick())
            await asyncio.wait_for(started.wait(), 5)
            await asyncio.wait_for(runner.aclose(), 2)
            assert task.done()
            result = await asyncio.wait_for(task, 1)
            assert runner._running is None
            assert runner._cancel_event is None
            assert runner._job_task is None
            return job, result

    job, result = asyncio.run(scenario())
    assert result["job"]["id"] == job["id"]
    assert result["job"]["status"] == "cancelled"
    assert result["job"]["error_code"] == "cancelled_by_owner"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_aclose_cleans_up_after_supervisor_cancels_its_worker(app, agent_id):
    from lingnexus.background import Supervisor

    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.sleep(30)  # the outer task cancellation must also close HTTP
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = EventSummaryRunner(app, client=client)
            enable(runner, agent_id, profile_id)
            runner.enqueue(event["id"])
            supervisor = Supervisor(app, foreground_busy=lambda: False)
            supervisor.event_summaries = runner
            # This is the shutdown order used by the application lifespan.
            supervisor._work_task = asyncio.create_task(supervisor.run_now())
            await asyncio.wait_for(started.wait(), 5)
            await asyncio.wait_for(supervisor.aclose(), 2)
            await asyncio.wait_for(runner.aclose(), 2)
            assert runner._job_task is None
            assert runner._running is None
            assert runner._cancel_event is None
            assert runner.jobs(event["id"])[0]["status"] == "retry"
            assert await runner.tick() == {"status": "closed", "processed": 0}
            assert client.is_closed is False

    asyncio.run(scenario())


def test_disabling_mid_job_prevents_any_activation(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    runner = runner_for(app, client_for([sse(summary_reply())]))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    claimed = runner._claim()
    runner.configure(agent_id, False, profile_id)
    outcome = asyncio.run(runner._run_job(claimed))
    assert outcome["status"] == "cancelled"
    assert outcome["error_code"] == "disabled"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_revoked_grant_stops_further_requests_mid_job(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    requests = []

    def hook(count):
        if count == 1:
            revoke(app, agent_id)  # the owner withdraws access after the first call

    runner = runner_for(
        app,
        client_for([sse(summary_reply("局部", ids[:1]))] * 4, requests, hook=hook),
    )
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    result = run(runner)
    assert len(requests) == 1, "a withdrawn grant must stop the next request"
    assert result["job"]["status"] == "stale"
    assert result["job"]["error_code"] == "event_changed"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_withdrawal_during_request_admission_sends_zero_http(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    requests = []
    runner = runner_for(app, client_for([sse(summary_reply("不应发送", ids[:1]))], requests))
    enable(runner, agent_id, profile_id, max_calls=1)
    runner.enqueue(event["id"])

    original = runner._revalidate
    checks = []

    def revoke_after_first_fence(job, current):
        checks.append(True)
        if len(checks) == 1:
            # The request guard yields next. Commit the withdrawal while its
            # first authorization snapshot has passed but HTTP has not started.
            asyncio.get_running_loop().call_soon(revoke, app, agent_id)
        original(job, current)

    runner._revalidate = revoke_after_first_fence
    result = run(runner)
    assert len(checks) == 2, "the post-yield fence must run before HTTP"
    assert requests == []
    assert result["job"]["status"] == "stale"
    assert result["job"]["error_code"] == "event_changed"
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_restart_reclaims_an_expired_lease_and_finishes_the_job(app_factory, app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    first = runner_for(app, client_for([sse(summary_reply())]))
    enable(first, agent_id, profile_id, max_calls=2)
    first.enqueue(event["id"])
    claimed = first._claim()
    assert claimed["status"] == "running"
    with app.database.transaction() as db:
        db.execute("UPDATE event_summary_jobs SET lease_until=1 WHERE id=?", (claimed["id"],))

    again = app_factory(app.database.path)
    try:
        # A fresh process has no in-memory key: the owner supplies it again at
        # startup, exactly as the real application boundary requires.
        stored = again.profiles.get(profile_id)["profile"]
        again.profiles.save(
            "organizer",
            ModelProfile.model_validate(stored),
            api_key="organizer-secret",
            profile_id=profile_id,
        )
        replies = [sse(summary_reply("下一次", ids[:1])), sse(summary_reply("合并后", ids[:1]))]
        restarted = runner_for(again, client_for(replies))
        assert restarted.enqueue(event["id"])["id"] == claimed["id"]
        result = run(restarted)
        assert result["job"]["outcome"] == "applied"
        assert result["job"]["attempts"] == 2
        assert GENERATED_MARKER in again.work_events.get(event["id"])["summary"]
    finally:
        again.close()


def test_old_holder_cannot_overwrite_a_new_lease(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    runner = runner_for(app, client_for([sse(summary_reply())]))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    stale = runner._claim()
    # A second holder takes the lease: the first holder lost its fence.
    with app.database.transaction() as db:
        db.execute(
            "UPDATE event_summary_jobs SET lease_token=? WHERE id=?",
            ("someone-else", stale["id"]),
        )
    outcome = asyncio.run(runner._run_job(stale))
    assert outcome.get("superseded") is True
    assert outcome["status"] == "running"  # the newer holder is untouched
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_stale_revision_cannot_overwrite_a_newer_summary(app, agent_id):
    profile_id = with_profile(app)
    _cid, event, _run = closed_event(app, agent_id)
    requests = []
    runner = runner_for(app, client_for([sse(summary_reply())], requests))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    claimed = runner._claim()
    owner = app.work_events.set_summary(
        event["id"], "所有者自己写的概况", expected_revision=event["revision"]
    )
    assert owner["summary_kind"] == "owner"
    outcome = asyncio.run(runner._run_job(claimed))
    assert outcome["status"] == "stale"
    assert outcome["error_code"] == "event_changed"
    assert requests == []  # nothing was sent after the revision changed
    final = app.work_events.get(event["id"])
    assert final["summary"] == "所有者自己写的概况"
    assert final["revision"] == owner["revision"]


def test_model_text_must_not_rewrite_the_authoritative_outcome(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    forged = summary_reply("一切都成功了", ids[:1], outcome="failed")
    runner = runner_for(app, client_for([sse(forged), sse(forged), sse(forged)]))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    result = run(runner)
    stored = app.work_events.get(event["id"])
    assert stored["outcome"] == event["outcome"] != "failed"
    assert result["job"]["outcome"] == "rejected"
    assert result["job"]["error_code"] == "outcome_mismatch"
    assert stored["summary_kind"] == "rule"


# --- bounds, batching and lifecycle ---------------------------------------


def test_single_call_budget_sends_one_batch_and_reports_partial(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id, body="很长的答复内容。" * 4000)
    ids = sources(app, agent_id, cid, event["id"])
    requests = []
    runner = runner_for(app, client_for([sse(summary_reply("压缩后", ids[:1]))], requests))
    enable(runner, agent_id, profile_id, max_calls=1)
    runner.enqueue(event["id"])
    result = run(runner)
    assert len(requests) == 1
    assert result["job"]["outcome"] == "applied"
    assert result["job"]["coverage"] == "partial"
    assert result["job"]["usage"]["calls"] == 1
    assert "只覆盖事件的一部分" in app.work_events.get(event["id"])["summary"]


def test_call_budget_is_global_across_retries(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    requests = []
    replies = [sse(summary_reply("局部", ids[:1])), 500]  # fragment ok, final fails
    runner = runner_for(app, client_for(replies, requests), retry_backoff_seconds=0)
    enable(runner, agent_id, profile_id, max_calls=2)
    runner.enqueue(event["id"])
    first_tick = run(runner)
    assert first_tick["job"]["status"] == "retry"
    assert len(requests) == 2
    second_tick = run(runner)
    # The budget is already spent; a retry may not restart it with new requests.
    assert len(requests) == 2
    assert second_tick["job"]["error_code"] == "call_budget"
    assert second_tick["job"]["status"] == "failed"


def test_long_source_truncation_is_reported_as_partial(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id, body="截断哨兵" * 6000)
    ids = sources(app, agent_id, cid, event["id"])
    runner = runner_for(
        app,
        client_for([sse(summary_reply("局部", ids[:1])), sse(summary_reply("最终", ids[:1]))]),
    )
    enable(runner, agent_id, profile_id, max_calls=2)
    runner.enqueue(event["id"])
    result = run(runner)
    assert result["job"]["outcome"] == "applied"
    assert result["job"]["coverage"] == "partial"
    assert "只覆盖事件的一部分" in app.work_events.get(event["id"])["summary"]


def test_foreground_busy_yields_before_claiming_and_between_calls(app, agent_id):
    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id)
    ids = sources(app, agent_id, cid, event["id"])
    requests = []
    runner = runner_for(
        app,
        client_for([sse(summary_reply("局部", ids[:1]))], requests),
        retry_backoff_seconds=0,
    )
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    busy = {"value": True}
    runner.foreground_busy = lambda: busy["value"]
    assert run(runner) == {"status": "foreground_busy", "processed": 0}
    assert requests == []
    assert runner.jobs(event["id"])[0]["status"] == "queued"

    # Report busy again after the first request: the batch must yield, not continue.
    def flip():
        if requests:
            busy["value"] = True
        return busy["value"]

    busy["value"] = False
    runner.foreground_busy = flip
    result = run(runner)
    assert result["job"]["status"] == "retry"
    assert result["job"]["error_code"] == "foreground_busy"
    assert len(requests) == 1
    assert app.work_events.get(event["id"])["summary_kind"] == "rule"


def test_aclose_only_closes_an_owned_client(app):
    client = client_for([sse(summary_reply())])
    injected = runner_for(app, client)
    asyncio.run(injected.aclose())
    assert client.is_closed is False
    owned = EventSummaryRunner(app)
    asyncio.run(owned.aclose())
    assert owned._client.is_closed is True


# --- the API/lifecycle seam -----------------------------------------------


def test_supervisor_drives_tick_and_reports_its_status(app, agent_id):
    from lingnexus.background import Supervisor

    profile_id = with_profile(app)
    cid, event, _run = closed_event(app, agent_id, body="原始答复")
    ids = sources(app, agent_id, cid, event["id"])
    replies = [sse(summary_reply("局部", ids[:1])), sse(summary_reply("最终", ids[:1]))]
    runner = runner_for(app, client_for(replies))
    enable(runner, agent_id, profile_id)
    runner.enqueue(event["id"])
    supervisor = Supervisor(app, foreground_busy=lambda: False)
    supervisor.event_summaries = runner
    result = asyncio.run(supervisor.run_now())
    assert result["status"] == "ok"
    assert result["event_summary"]["processed"] == 1
    asyncio.run(supervisor.aclose())


def test_runner_is_reachable_from_the_lifecycle_entry_points(app):
    from lingnexus.api import app as api_app_module

    assert api_app_module.EventSummaryRunner is EventSummaryRunner
