"""Task memory fences use real SQLite/services; only model HTTP is synthetic."""

import asyncio
import json
from threading import Event, Thread

import httpx
import pytest
from fastapi import FastAPI
from test_tasks import chat, client_for, configure, settle

from lingnexus.common.types import new_id
from lingnexus.memory.maintenance import MemoryMaintenance
from lingnexus.tasks import TaskRuntime
from lingnexus.tasks.protocol import append_response


def entry(app, agent_id, kind="knowledge", scope="real"):
    ref = app.history.add(
        agent_id,
        "Meeting Friday, original observation.",
        kind="observation" if scope == "external" else "user",
        scope=scope,
        request_id=new_id(),
    )
    if kind == "knowledge":
        item_id = app.memory.knowledge.save(
            agent_id,
            "Meeting Friday",
            [ref],
            kind="fact",
            scope=scope,
            request_id=new_id(),
        )
    else:
        item_id = app.memory.save_episode(
            agent_id,
            "Meeting",
            "Meeting Friday",
            [ref],
            scope=scope,
            request_id=new_id(),
        )
    return item_id, ref


def recall(name, item_id, ref):
    args = {"query": "Meeting"}
    if name == "read_episode":
        args = {"episode_id": item_id}
    elif name == "read_source":
        args = {"source_id": ref.id, "revision": 1, "offset": 8, "limit": 6}
    return ("recall", name, args)


def mutate(app, kind, item_id, ref, change):
    maintenance = MemoryMaintenance(app)
    if change == "correct":
        fields = (
            {"statement": "Meeting Saturday"}
            if kind == "knowledge"
            else {"summary": "Meeting Saturday"}
        )
        maintenance.correct(kind, item_id, expected_revision=1, **fields)
    elif change in {"archive", "delete"}:
        maintenance.set_state(kind, item_id, expected_revision=1, action=change)
    elif change == "source_correct":
        app.history.correct(ref, "Meeting Saturday, corrected observation.")
    else:
        app.history.set_visible(ref, False)


def assert_blocked(runtime, tid):
    result = runtime.get(tid)
    assert result["status"] == "blocked", result
    assert result["error_code"] == "memory_changed", result
    assert result["verified"] is False and result["final"] is None
    assert runtime.store.load(tid)["verified"] is False
    assert not any(e["type"] == "final" for e in runtime.events(tid))
    with runtime.app.database.transaction() as db:
        assert not db.execute(
            "SELECT 1 FROM activity_events WHERE dedupe_key=?", ("task:" + tid,)
        ).fetchone()


def test_owner_api_correction_during_final_await_requires_new_task(app, agent_id):
    async def run():
        from lingnexus.api.memory import router as memory_router
        from lingnexus.api.tasks import router as tasks_router

        configure(app)
        item_id, ref = entry(app, agent_id)
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            assert getattr(app.database._local, "connection", None) is None
            if len(requests) in {1, 3}:
                text = chat(calls=[recall("find_knowledge", item_id, ref)])
            elif len(requests) == 2:
                assert "Meeting Friday" in body["messages"][-1]["content"]
                entered.set()
                await release.wait()
                text = chat("Meeting Friday, verified.")
            else:
                assert "Meeting Saturday" in body["messages"][-1]["content"]
                text = chat("Meeting Saturday, verified.")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as model:
            runtime = TaskRuntime(app, model)
            server = FastAPI()
            server.state.services, server.state.tasks = app, runtime
            server.state.owner_token, server.state.allowed_origins = "owner-token", []
            server.include_router(memory_router)
            server.include_router(tasks_router)
            headers = {"Authorization": "Bearer owner-token"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server), base_url="http://local", headers=headers
            ) as owner:
                spec = {
                    "agent_id": agent_id,
                    "objective": "When is the meeting?",
                    "request_id": "old-friday",
                }
                submitted = await owner.post("/tasks", json=spec)
                assert submitted.status_code == 202
                tid = submitted.json()["id"]
                await asyncio.wait_for(entered.wait(), 3)
                corrected = await owner.patch(
                    "/memory/knowledge/" + item_id,
                    json={"expected_revision": 1, "statement": "Meeting Saturday"},
                )
                assert corrected.status_code == 200 and corrected.json()["revision"] == 2
                release.set()
                await settle(runtime, tid)
                assert_blocked(runtime, tid)
                resumed = await owner.post(f"/tasks/{tid}/resume")
                assert resumed.status_code == 200
                assert resumed.json()["error_code"] == "memory_changed"
                steered = await owner.post(f"/tasks/{tid}/steer", json={"text": "Use Saturday"})
                assert steered.status_code == 200
                assert steered.json()["status"] == "blocked"
                assert_blocked(runtime, tid)
                assert len(requests) == 2
                assert (await owner.post("/tasks", json=spec)).json()["id"] == tid
                fresh = await owner.post("/tasks", json={**spec, "request_id": "fresh-saturday"})
                outcome = await settle(runtime, fresh.json()["id"])
                assert outcome["status"] == "completed" and outcome["verified"] is True
                assert outcome["final"] == "Meeting Saturday, verified."
                pinned = runtime.store.load(tid)
                assert pinned["memory_refs"] == [
                    {"kind": "knowledge", "id": item_id, "revision": 1}
                ]
            await runtime.close()

    asyncio.run(run())


def test_recovered_completed_file_result_pins_its_actual_attached_source(app, agent_id, tmp_path):
    async def run():
        configure(app)
        target = tmp_path / "observed.txt"
        target.write_text("Actual contents")
        app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
        requests = []
        async with client_for([chat("Checked")], requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Read file", "request_id": new_id()}
            )
            await runtime.pause(tid)
            call = {"id": "file", "name": "read_file", "arguments": {"path": str(target)}}
            state = runtime.store.load(tid)
            append_response(
                state["payload"],
                state["family"],
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "file",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": json.dumps(call["arguments"]),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
            state["pending"] = [call]
            runtime.store.save(tid, state, model_step=True)
            result = app.actions.execute(
                agent_id, f"task:{tid}:1:file", "read_file", call["arguments"]
            )
            step = runtime.store.step(tid, 1, call)
            # A pre-fence result already persisted before its awaiter checkpointed.
            runtime.store.result(step["id"], result, "completed")
            await runtime.resume(tid)
            assert (await settle(runtime, tid))["status"] == "completed"
            assert runtime.store.load(tid)["source_refs"] == [result["source"].model_dump()]
            await runtime.close()

    asyncio.run(run())


def test_recalled_text_and_marker_share_sqlite_snapshot_during_owner_write(
    app, agent_id, app_factory, monkeypatch
):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id)
        other = app_factory(app.database.path)
        attempted, finished = Event(), Event()
        errors, connections = [], []

        def correct():
            attempted.set()
            try:
                mutate(other, "knowledge", item_id, ref, "correct")
            except BaseException as error:  # noqa: BLE001 -- surface writer failures in the test
                errors.append(error)
            finally:
                finished.set()

        writer = Thread(target=correct)
        search, marker = app.memory.knowledge.search, MemoryMaintenance.snapshot_marker

        def read_then_contend(*args, **kwargs):
            items = search(*args, **kwargs)
            if items and not connections:
                connection = getattr(app.database._local, "connection", None)
                assert connection is not None, "Content read escaped the capture transaction"
                connections.append(connection)
                writer.start()
                assert attempted.wait(1)
                assert not finished.is_set()
            return items

        def capture(self, kind, identifier):
            assert getattr(app.database._local, "connection", None) is connections[0]
            result = marker(self, kind, identifier)
            assert result["revision"] == 1 and not finished.is_set()
            return result

        monkeypatch.setattr(app.memory.knowledge, "search", read_then_contend)
        monkeypatch.setattr(MemoryMaintenance, "snapshot_marker", capture)
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            assert getattr(app.database._local, "connection", None) is None
            if len(requests) == 1:
                text = chat(calls=[recall("find_knowledge", item_id, ref)])
            else:
                entered.set()
                await release.wait()
                text = chat("Friday")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            # The writer may commit before the next model starts; both boundaries reject it.
            await asyncio.to_thread(writer_finished, attempted, writer)
            assert not errors
            release.set()
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            assert MemoryMaintenance(app).read("knowledge", item_id)["revision"] == 2
            state = runtime.store.load(tid)
            assert state["memory_refs"] == [{"kind": "knowledge", "id": item_id, "revision": 1}]
            await runtime.close()
        other.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,kind",
    [
        ("read_source", "knowledge"),
        ("read_episode", "episodes"),
        ("find_knowledge", "knowledge"),
        ("find_memories", "episodes"),
    ],
)
def test_source_invalid_before_dispatch_blocks_instead_of_failed_tool_retries(
    app, agent_id, name, kind
):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id, kind)
        app.history.set_visible(ref, False)
        if name in {"find_knowledge", "find_memories"}:
            # Legacy active index rows must still honor the original source fence.
            with app.database.transaction() as db:
                db.execute(f"UPDATE {kind} SET status='active' WHERE id=?", (item_id,))
        requests = []
        async with client_for([chat(calls=[recall(name, item_id, ref)])], requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            assert len(requests) == 1
            await runtime.resume(tid)
            assert_blocked(runtime, tid)
            assert len(requests) == 1
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("name", ["read_source", "find_knowledge"])
def test_original_provenance_is_captured_before_actions_own_tool_source(
    app, agent_id, monkeypatch, name
):
    async def run():
        from lingnexus.tasks import execution

        configure(app)
        item_id, ref = entry(app, agent_id, scope="external")
        captured = []
        native_capture, native_finish = execution.capture_memory, app.actions._finish

        def capture(*args):
            native_capture(*args)
            result = args[-1]
            assert "source" not in result
            captured.append((result["memory_refs"], result["source_refs"]))

        def finish(intent, status, result, error_code):
            if intent["name"] == name and status == "completed":
                assert captured, "Original references must precede Actions' observation"
                markers, sources = captured[-1]
                assert sources == [ref.model_dump()]
                assert markers == (
                    []
                    if name == "read_source"
                    else [{"kind": "knowledge", "id": item_id, "revision": 1}]
                )
            return native_finish(intent, status, result, error_code)

        monkeypatch.setattr(execution, "capture_memory", capture)
        monkeypatch.setattr(app.actions, "_finish", finish)
        async with client_for(
            [chat(calls=[recall(name, item_id, ref)]), chat("Friday")], []
        ) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            result = await settle(runtime, tid)
            assert result["status"] == "completed" and result["verified"]
            await runtime.close()

    asyncio.run(run())


def writer_finished(attempted, writer):
    assert attempted.wait(3)
    writer.join(3)
    assert not writer.is_alive()


def test_memory_capture_keeps_actions_before_database_lock_order(app, agent_id, monkeypatch):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id)
        invoke = app.actions._invoke

        def read_with_competing_action(*args):
            acquired = []

            def compete():
                obtained = app.actions._lock.acquire(blocking=False)
                acquired.append(obtained)
                if obtained:
                    app.actions._lock.release()

            competitor = Thread(target=compete)
            competitor.start()
            competitor.join(1)
            assert acquired == [False], "A competing file Action could deadlock at _finish"
            return invoke(*args)

        monkeypatch.setattr(app.actions, "_invoke", read_with_competing_action)
        async with client_for(
            [chat(calls=[recall("find_knowledge", item_id, ref)]), chat("Friday")], []
        ) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            result = await settle(runtime, tid)
            assert result["status"] == "completed" and result["verified"]
            await runtime.close()

    asyncio.run(run())


def test_model_fake_ids_never_become_checkpoint_evidence(app, agent_id):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id)
        fake = new_id()
        requests = []
        replies = [
            chat(
                calls=[
                    ("fake", "read_source", {"source_id": fake, "revision": 1}),
                    recall("find_knowledge", item_id, ref),
                ]
            ),
            chat(
                json.dumps(
                    {
                        "memory_refs": [{"kind": "knowledge", "id": fake, "revision": 900}],
                        "source_refs": [{"id": fake, "revision": 900}],
                    }
                )
            ),
        ]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            result = await settle(runtime, tid)
            assert result["status"] == "needs_review" and not result["verified"]
            state = runtime.store.load(tid)
            assert state["memory_refs"] == [{"kind": "knowledge", "id": item_id, "revision": 1}]
            assert ref.model_dump() in state["source_refs"]
            assert fake not in {r["id"] for r in state["source_refs"]}
            assert not state.get("memory_changed")
            await runtime.close()

    asyncio.run(run())


def test_branch_reactivation_cannot_unblock_an_old_native_conversation(app, agent_id):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id, scope="external")
        requests = []
        async with client_for(
            [chat(calls=[recall("read_source", item_id, ref)])], requests
        ) as client:
            runtime = TaskRuntime(app, client, max_steps=1)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            assert (await settle(runtime, tid))["status"] == "paused"
            app.history.set_visible(ref, False)
            await runtime.resume(tid, additional_steps=1)
            assert_blocked(runtime, tid)
            app.history.set_visible(ref, True)
            await runtime.pause(tid)
            await runtime.resume(tid, additional_steps=1)
            assert_blocked(runtime, tid)
            assert len(requests) == 1
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "family", ["openai_chat_completions", "anthropic_messages", "openai_responses"]
)
def test_native_families_reject_stale_memory_before_persisting_continuation(app, agent_id, family):
    async def run():
        from test_task_dependencies import native_reply

        configure(app, family)
        item_id, ref = entry(app, agent_id)
        requests = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                text = native_reply(family, calls=[recall("find_knowledge", item_id, ref)])
            else:
                entered.set()
                await release.wait()
                text = native_reply(family, text="Friday")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await asyncio.wait_for(entered.wait(), 3)
            mutate(app, "knowledge", item_id, ref, "correct")
            release.set()
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            state = runtime.store.load(tid)
            key = "input" if family == "openai_responses" else "messages"
            assert state["payload"][key] == requests[1][key]
            await runtime.close()

    asyncio.run(run())


def test_file_operation_releases_db_transaction_and_pins_tool_observation(
    app, agent_id, tmp_path, monkeypatch
):
    async def run():
        configure(app)
        target = tmp_path / "actual.txt"
        target.write_text("Actual contents")
        app.permissions.configure(agent_id, readable=[tmp_path], writable=[], expected_revision=0)
        read_text = app.permissions.read_text

        def read_without_transaction(*args):
            assert getattr(app.database._local, "connection", None) is None
            return read_text(*args)

        monkeypatch.setattr(app.permissions, "read_text", read_without_transaction)
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                text = chat(calls=[("file", "read_file", {"path": str(target)})])
            else:
                entered.set()
                await release.wait()
                text = chat("Checked")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Read file", "request_id": new_id()}
            )
            await asyncio.wait_for(entered.wait(), 3)
            actual = json.loads(requests[1]["messages"][-1]["content"])
            from lingnexus.common.types import SourceRef

            app.history.set_visible(SourceRef(**actual["source"]), False)
            release.set()
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            assert target.read_text() == "Actual contents"
            await runtime.close()

    asyncio.run(run())


def test_old_memory_checkpoint_cannot_be_freshened_on_resume(app, agent_id):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id)
        async with client_for([], []) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await runtime.pause(tid)
            call = {"id": "legacy", "name": "find_knowledge", "arguments": {"query": "Meeting"}}
            old = app.actions.execute(agent_id, "legacy-recall", call["name"], call["arguments"])
            step = runtime.store.step(tid, 1, call)
            runtime.store.result(step["id"], old, "completed")
            state = runtime.store.load(tid)
            state.pop("memory_refs")
            state.pop("source_refs")
            state["verified"] = True
            runtime.store.save(tid, state)
            mutate(app, "knowledge", item_id, ref, "correct")
            await runtime.resume(tid)
            assert_blocked(runtime, tid)
            assert not runtime.store.load(tid).get("memory_refs")
            await runtime.close()

    asyncio.run(run())


def test_recovered_action_keeps_old_revision_instead_of_current_marker(app, agent_id):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id)
        requests = []
        async with client_for([], requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await runtime.pause(tid)
            call = {"id": "recover", "name": "find_knowledge", "arguments": {"query": "Meeting"}}
            state = runtime.store.load(tid)
            state["pending"] = [call]
            runtime.store.save(tid, state, model_step=True)
            old = app.actions.execute(
                agent_id, f"task:{tid}:1:recover", call["name"], call["arguments"]
            )
            step = runtime.store.step(tid, 1, call)
            runtime.store.executing(step["id"], old["id"])
            mutate(app, "knowledge", item_id, ref, "correct")
            await runtime.resume(tid)
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            state = runtime.store.load(tid)
            assert state["memory_refs"] == [{"kind": "knowledge", "id": item_id, "revision": 1}]
            assert state["pending"] == [] and not requests
            actual = json.loads(state["payload"]["messages"][-1]["content"])
            assert actual["result"]["knowledge"][0]["statement"] == "Meeting Friday"
            await runtime.close()

    asyncio.run(run())


def test_recovered_stale_action_retains_every_actual_returned_item_and_source(app, agent_id):
    async def run():
        configure(app)
        real_id, real_ref = entry(app, agent_id)
        external_id, external_ref = entry(app, agent_id, scope="external")
        async with client_for([], []) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await runtime.pause(tid)
            call = {"id": "recover", "name": "find_knowledge", "arguments": {"query": "Meeting"}}
            state = runtime.store.load(tid)
            state["pending"] = [call]
            runtime.store.save(tid, state, model_step=True)
            app.actions.execute(agent_id, f"task:{tid}:1:recover", call["name"], call["arguments"])
            mutate(app, "knowledge", real_id, real_ref, "correct")
            await runtime.resume(tid)
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            state = runtime.store.load(tid)
            assert {"kind": "knowledge", "id": real_id, "revision": 1} in state["memory_refs"]
            assert {"kind": "knowledge", "id": external_id, "revision": 1} in state["memory_refs"]
            assert len(state["memory_refs"]) == 2
            assert real_ref.model_dump() in state["source_refs"]
            assert external_ref.model_dump() in state["source_refs"]
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,kind",
    [
        ("find_knowledge", "knowledge"),
        ("find_memories", "episodes"),
        ("read_episode", "episodes"),
    ],
)
@pytest.mark.parametrize("change", ["correct", "archive", "delete", "source_correct", "branch"])
@pytest.mark.parametrize("output", ["final", "plan"])
def test_memory_changes_during_model_await_discard_final_and_tool_plan(
    app, agent_id, tmp_path, name, kind, change, output
):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id, kind)
        target = tmp_path / "stale-plan.txt"
        app.permissions.configure(
            agent_id, readable=[tmp_path], writable=[tmp_path], expected_revision=0
        )
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                text = chat(calls=[recall(name, item_id, ref)])
            else:
                assert len(requests) == 2, "Stale continuation requested another model step"
                entered.set()
                await release.wait()
                text = chat(
                    "Meeting Friday",
                    [("stale-write", "create_file", {"path": str(target), "text": "Friday"})]
                    if output == "plan"
                    else [],
                )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await asyncio.wait_for(entered.wait(), 3)
            mutate(app, kind, item_id, ref, change)
            release.set()
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            assert not target.exists()
            state = runtime.store.load(tid)
            assert state["pending"] == []
            assert state["payload"]["messages"][-1]["role"] == "tool"
            assert state["memory_refs"] == [{"kind": kind, "id": item_id, "revision": 1}]
            await runtime.resume(tid)
            assert_blocked(runtime, tid)
            assert len(requests) == 2
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["source_correct", "branch"])
def test_exact_source_slice_is_invalidated_after_model_await(app, agent_id, change):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id, scope="external")
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                text = chat(calls=[recall("read_source", item_id, ref)])
            else:
                entered.set()
                await release.wait()
                text = chat("Friday")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            await asyncio.wait_for(entered.wait(), 3)
            mutate(app, "knowledge", item_id, ref, change)
            release.set()
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            assert ref.model_dump() in runtime.store.load(tid)["source_refs"]
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["before_model", "before_tool", "tool_return", "memory_return"])
def test_memory_fence_checks_tool_boundaries_and_retains_actual_effects(
    app, agent_id, tmp_path, stage
):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id)
        target = tmp_path / "actual-effect.txt"
        app.permissions.configure(
            agent_id, readable=[tmp_path], writable=[tmp_path], expected_revision=0
        )
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []
        replies = [
            chat(calls=[recall("find_knowledge", item_id, ref)]),
            chat(calls=[("write", "create_file", {"path": str(target), "text": "Friday"})]),
            chat("Friday"),
        ]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            original = runtime.executor.execute

            async def execute(task, call):
                if stage == "before_tool" and call["name"] == "create_file":
                    mutate(app, "knowledge", item_id, ref, "correct")
                result = await original(task, call)
                gate = call["name"] == (
                    "create_file" if stage == "tool_return" else "find_knowledge"
                )
                if stage in {"tool_return", "memory_return"} and gate:
                    entered.set()
                    await release.wait()
                elif stage == "before_model" and call["name"] == "find_knowledge":
                    # The result remains factual; foreground priority holds the next step.
                    busy[0] = True
                    entered.set()
                return result

            busy = [False]
            runtime.foreground_busy = lambda: busy[0]
            runtime.executor.execute = execute
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            if stage != "before_tool":
                await asyncio.wait_for(entered.wait(), 3)
                if stage == "before_model":
                    await runtime.pause(tid)
                mutate(app, "knowledge", item_id, ref, "correct")
                release.set()
                busy[0] = False
                if stage == "before_model":
                    await runtime.resume(tid)
            await settle(runtime, tid)
            assert_blocked(runtime, tid)
            state = runtime.store.load(tid)
            assert state["memory_refs"] == [{"kind": "knowledge", "id": item_id, "revision": 1}]
            if stage == "tool_return":
                assert target.read_text() == "Friday"
                assert state["pending"] == []
                assert state["payload"]["messages"][-1]["tool_call_id"] == "write"
                events = [e for e in runtime.events(tid) if e["type"] == "tool_result"]
                assert events[-1]["data"]["result"]["status"] == "completed"
            else:
                assert not target.exists()
            assert len(requests) == (2 if stage in {"before_tool", "tool_return"} else 1)
            await runtime.resume(tid)
            assert_blocked(runtime, tid)
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,kind",
    [
        ("find_knowledge", "knowledge"),
        ("find_memories", "episodes"),
        ("read_episode", "episodes"),
        ("read_source", "knowledge"),
    ],
)
@pytest.mark.parametrize("scope", ["real", "external"])
def test_actual_returned_memory_and_sources_are_pinned_and_valid_scopes_complete(
    app, agent_id, name, kind, scope
):
    async def run():
        configure(app)
        item_id, ref = entry(app, agent_id, kind, scope)
        requests = []
        replies = [chat(calls=[recall(name, item_id, ref)]), chat("Meeting Friday")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit(
                {"agent_id": agent_id, "objective": "Meeting", "request_id": new_id()}
            )
            result = await settle(runtime, tid)
            assert result["status"] == "completed" and result["verified"] is True
            state = runtime.store.load(tid)
            assert state["memory_refs"] == (
                [] if name == "read_source" else [{"kind": kind, "id": item_id, "revision": 1}]
            )
            actual = json.loads(requests[1]["messages"][-1]["content"])
            assert {tuple(sorted(r.items())) for r in state["source_refs"]} == {
                tuple(sorted(ref.model_dump().items())),
                tuple(sorted(actual["source"].items())),
            }
            if name == "read_source":
                assert actual["result"]["text"] == "Friday"
                assert actual["result"]["offset"] == 8 and actual["result"]["has_more"]
            await runtime.close()

    asyncio.run(run())
