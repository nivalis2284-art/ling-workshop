"""Task dependency requests are data; owner installs and model calls are mocked."""

import asyncio
import json

import pytest
from test_dependencies import boundary as dependency_boundary
from test_tasks import anthropic, chat, client_for, configure, settle, sse

from lingnexus.common.errors import Conflict
from lingnexus.dependencies import DependencyCandidate, DependencyService
from lingnexus.tasks import TaskRuntime


@pytest.fixture
def boundary(monkeypatch):
    return dependency_boundary.__wrapped__(monkeypatch)


def project_for(app, agent_id, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print(42)\n")
    app.permissions.configure(agent_id, readable=[project], writable=[project], expected_revision=0)
    return project


def native_reply(family, calls=(), text=None):
    if family == "openai_chat_completions":
        return chat(text, calls)
    if family == "anthropic_messages":
        blocks = [{"type": "thinking", "thinking": "private", "signature": "opaque"}]
        blocks += [
            {"type": "tool_use", "id": cid, "name": name, "input": args}
            for cid, name, args in calls
        ]
        if text:
            blocks.append({"type": "text", "text": text})
        return anthropic(blocks)
    output = [
        {"type": "reasoning", "id": "reasoning-item", "encrypted_content": "opaque", "summary": []}
    ]
    output += [
        {
            "type": "function_call",
            "id": "item-" + cid,
            "call_id": cid,
            "name": name,
            "arguments": json.dumps(args),
        }
        for cid, name, args in calls
    ]
    if text:
        output.append(
            {
                "type": "message",
                "id": "message-item",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    return sse(
        [
            {
                "type": "response.completed",
                "response": {"id": "response-fixture", "status": "completed", "output": output},
            }
        ]
    )


@pytest.mark.parametrize(
    "family", ["openai_chat_completions", "anthropic_messages", "openai_responses"]
)
@pytest.mark.parametrize(
    "manager,packages", [("pip", ["requests==2.32.3"]), ("npm", ["lodash@4.17.21"])]
)
def test_request_blocks_then_owner_install_resume_keeps_native_calls(
    app, agent_id, tmp_path, boundary, family, manager, packages
):
    async def run():
        configure(app, family)
        project = project_for(app, agent_id, tmp_path)
        candidate = {"manager": manager, "packages": packages}
        requests = []
        # A sibling must wait across the owner checkpoint and verify the new environment.
        replies = [
            native_reply(
                family,
                [
                    ("dependency-native", "request_dependencies", candidate),
                    (
                        "verify-native",
                        "run_command",
                        {"argv": ["python", "-V"], "purpose": "verify"},
                    ),
                ],
            ),
            native_reply(family, text="Checked the configured environment."),
        ]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client, docker_image="fixture:approved")
            service = app.dependencies = DependencyService(app, runtime)
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Check project",
                        "request_id": "dependency",
                        "workspace": str(project),
                    }
                )
                task = await settle(runtime, tid)
                assert task["status"] == "blocked" and task["error_code"] == "dependencies_required"
                assert task["dependency_candidate"] == candidate and not task["verified"]
                assert len(requests) == 1 and boundary.calls == [] and service.list(tid) == []
                state = runtime.store.load(tid)
                assert [call["id"] for call in state["pending"]] == ["verify-native"]
                assert state["failures"] == {} and not state["command_verified"]
                event = next(e for e in runtime.events(tid) if e["type"] == "tool_result")
                assert event["data"]["result"]["status"] == "completed"
                assert event["data"]["result"]["dependency_candidate"] == candidate
                await runtime.resume(tid)
                assert runtime.get(tid)["error_code"] == "dependencies_required"
                assert len(requests) == 1 and boundary.calls == []
                plan = service.prepare(tid, **candidate, request_id="owner-plan")
                await runtime.resume(tid)
                assert runtime.get(tid)["status"] == "blocked" and len(requests) == 1
                # Pending resume must not change the immutable plan checkpoint binding.
                installed = await service.approve(plan["id"], intent_hash=plan["intent_hash"])
                assert installed["status"] == "success", installed
                await runtime.close()
                runtime = TaskRuntime(app, client)
                service = app.dependencies = DependencyService(app, runtime)
                await runtime.start()
                await runtime.resume(tid)
                task = await settle(runtime, tid)
                assert task["status"] == "completed" and task["verified"], task
                assert len(requests) == 2
                assert (
                    len(
                        [
                            e
                            for e in runtime.events(tid)
                            if e["type"] == "tool_started"
                            and e["data"]["name"] == "request_dependencies"
                        ]
                    )
                    == 1
                )
                body = requests[1]
                transcript = body.get("messages", body.get("input"))
                assert transcript[-1]["role"] == "user"
                status = json.loads(transcript[-1]["content"])
                assert status["dependency_statuses"][0]["status"] == "success"
                assert status["dependency_statuses"][0]["packages"] == packages
                assert "binding" not in transcript[-1]["content"]
                if family == "openai_chat_completions":
                    outputs = [m for m in transcript if m["role"] == "tool"]
                    assert [m["tool_call_id"] for m in outputs] == [
                        "dependency-native",
                        "verify-native",
                    ]
                elif family == "anthropic_messages":
                    assert transcript[1]["content"][0]["signature"] == "opaque"
                    outputs = [
                        b
                        for m in transcript
                        if isinstance(m["content"], list)
                        for b in m["content"]
                        if b["type"] == "tool_result"
                    ]
                    assert [b["tool_use_id"] for b in outputs] == [
                        "dependency-native",
                        "verify-native",
                    ]
                    assert all(not b["is_error"] for b in outputs)
                else:
                    assert transcript[1]["encrypted_content"] == "opaque"
                    outputs = [m for m in transcript if m.get("type") == "function_call_output"]
                    assert [m["call_id"] for m in outputs] == ["dependency-native", "verify-native"]
                    assert transcript[2]["id"] == "item-dependency-native"
                creates = [c for c in boundary.calls if c[1] == "create"]
                assert [c[c.index("--network") + 1] for c in creates] == (
                    ["bridge", "none", "none"] if manager == "pip" else ["bridge", "none"]
                )
            finally:
                await runtime.close()
                await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "args",
    [
        {"manager": "pip", "packages": ["requests"]},
        {"manager": "npm", "packages": ["lodash@latest"]},
        {"manager": "pip", "packages": ["x==1.0"], "approve": True},
        {"manager": "uv", "packages": ["x==1.0"]},
    ],
)
def test_invalid_dependency_request_returns_failure_without_owner_block(
    app, agent_id, boundary, args
):
    async def run():
        configure(app)
        requests = []
        async with client_for(
            [chat(calls=[("bad", "request_dependencies", args)]), chat("Cannot verify.")], requests
        ) as client:
            runtime = TaskRuntime(app, client)
            try:
                tid = await runtime.submit(
                    {"agent_id": agent_id, "objective": "Check", "request_id": "invalid"}
                )
                task = await settle(runtime, tid)
                assert task["status"] == "needs_review" and len(requests) == 2
                assert boundary.calls == [] and not task.get("dependency_candidate")
                result = json.loads(requests[1]["messages"][-1]["content"])
                assert result["error_code"] == "invalid_tool_request"
                assert runtime.store.load(tid)["failures"]
            finally:
                await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["pending", "failed", "running", "unknown"])
def test_resume_requires_matching_success_and_respects_effect_fence(
    app, agent_id, tmp_path, boundary, status
):
    async def run():
        configure(app)
        project = project_for(app, agent_id, tmp_path)
        candidate = {"manager": "pip", "packages": ["requests==2.32.3"]}
        requests = []
        async with client_for(
            [chat(calls=[("deps", "request_dependencies", candidate)])], requests
        ) as client:
            runtime = TaskRuntime(app, client, docker_image="fixture:approved")
            service = app.dependencies = DependencyService(app, runtime)
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Check",
                        "request_id": "fence",
                        "workspace": str(project),
                    }
                )
                assert (await settle(runtime, tid))["error_code"] == "dependencies_required"
                plan = service.prepare(tid, **candidate, request_id="owner")
                if status != "pending":
                    service.store.result(plan["id"], status, error="fixture_failure")
                boundary.calls.clear()
                if status in {"running", "unknown"}:
                    with pytest.raises(Conflict):
                        await runtime.resume(tid)
                else:
                    await runtime.resume(tid)
                assert runtime.get(tid)["status"] == "blocked" and len(requests) == 1
                assert boundary.calls == []
            finally:
                await runtime.close()
                await service.aclose()

    asyncio.run(run())


def test_catalog_reuses_candidate_schema():
    from lingnexus.tasks.protocol import catalog

    descriptor = next(d for d in catalog() if d["name"] == "request_dependencies")
    assert descriptor["parameters"] == DependencyCandidate.model_json_schema()
    assert set(descriptor["parameters"]["properties"]) == {"manager", "packages"}
    assert not any(
        d["name"] in {"prepare_dependencies", "approve_dependencies", "install"} for d in catalog()
    )


@pytest.mark.parametrize("requested", [True, False])
def test_dependency_changes_require_fresh_command_verification(
    app, agent_id, tmp_path, boundary, requested
):
    async def run():
        configure(app)
        project = project_for(app, agent_id, tmp_path)
        candidate = {"manager": "pip", "packages": ["requests==2.32.3"]}
        requests = []
        calls = [("old-check", "run_command", {"argv": ["python", "-V"], "purpose": "verify"})]
        if requested:
            calls.append(("deps", "request_dependencies", candidate))
        replies = [
            chat(calls=calls),
            chat(calls=[("read", "read_file", {"path": "main.py"})]),
            chat("All checks passed."),
        ]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client, docker_image="fixture:approved", max_steps=1)
            service = app.dependencies = DependencyService(app, runtime)
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Check",
                        "request_id": "fresh",
                        "workspace": str(project),
                    }
                )
                task = await settle(runtime, tid)
                assert task["status"] == ("blocked" if requested else "paused")
                assert runtime.store.load(tid)["verified"] is (not requested)
                plan = service.prepare(tid, **candidate, request_id="owner")
                assert (await service.approve(plan["id"], intent_hash=plan["intent_hash"]))[
                    "status"
                ] == "success"
                await runtime.resume(tid, additional_steps=2)
                task = await settle(runtime, tid)
                assert task["status"] == "needs_review" and not task["verified"], task
                assert runtime.store.load(tid)["failures"] == {}
                assert (
                    len(
                        [
                            e
                            for e in runtime.events(tid)
                            if e["type"] == "tool_result" and e["data"]["name"] == "run_command"
                        ]
                    )
                    == 1
                )
            finally:
                await runtime.close()
                await service.aclose()

    asyncio.run(run())


def test_success_with_wrong_version_does_not_release_dependency_request(
    app, agent_id, tmp_path, boundary
):
    async def run():
        configure(app)
        project = project_for(app, agent_id, tmp_path)
        candidate = {"manager": "pip", "packages": ["requests==2.32.3"]}
        requests = []
        async with client_for(
            [chat(calls=[("deps", "request_dependencies", candidate)]), chat("Not verified yet.")],
            requests,
        ) as client:
            runtime = TaskRuntime(app, client, docker_image="fixture:approved")
            service = app.dependencies = DependencyService(app, runtime)
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Check",
                        "request_id": "version",
                        "workspace": str(project),
                    }
                )
                assert (await settle(runtime, tid))["status"] == "blocked"
                wrong = service.prepare(
                    tid, manager="pip", packages=["requests==2.31.0"], request_id="wrong"
                )
                assert (await service.approve(wrong["id"], intent_hash=wrong["intent_hash"]))[
                    "status"
                ] == "success"
                await runtime.resume(tid)
                assert runtime.get(tid)["status"] == "blocked" and len(requests) == 1
                right = service.prepare(tid, **candidate, request_id="right")
                assert (await service.approve(right["id"], intent_hash=right["intent_hash"]))[
                    "status"
                ] == "success"
                await runtime.resume(tid)
                assert (await settle(runtime, tid))["status"] == "needs_review"
                assert len(requests) == 2
                statuses = json.loads(requests[1]["messages"][-1]["content"])["dependency_statuses"]
                assert [p["status"] for p in statuses] == ["success", "success"]
            finally:
                await runtime.close()
                await service.aclose()

    asyncio.run(run())


def test_request_without_dependency_service_stays_blocked_and_replays_data_safely(
    app, agent_id, boundary
):
    async def run():
        from lingnexus.tasks.protocol import append_response

        configure(app)
        requests = []
        candidate = {"manager": "npm", "packages": ["lodash@4.17.21"]}
        async with client_for([], requests) as client:
            runtime = TaskRuntime(app, client, foreground_busy=lambda: True)
            try:
                tid = await runtime.submit(
                    {"agent_id": agent_id, "objective": "Check", "request_id": "pure"}
                )
                await runtime.pause(tid)
                call = {"id": "replayed", "name": "request_dependencies", "arguments": candidate}
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
                                            "id": "replayed",
                                            "type": "function",
                                            "function": {
                                                "name": call["name"],
                                                "arguments": json.dumps(candidate),
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
                step = runtime.store.step(tid, 1, call)
                runtime.store.executing(step["id"])
                runtime.foreground_busy = lambda: False
                await runtime.resume(tid)
                assert (await settle(runtime, tid))["error_code"] == "dependencies_required"
                assert runtime.get(tid)["dependency_candidate"] == candidate
                assert runtime.store.load(tid)["failures"] == {}
                await runtime.resume(tid)
                assert runtime.get(tid)["status"] == "blocked"
                assert requests == [] and boundary.calls == [] and app.dependencies is None
            finally:
                await runtime.close()

    asyncio.run(run())


def test_steering_drains_active_tool_and_persists_before_next_model(app, agent_id, tmp_path):
    async def run():
        configure(app)
        project = project_for(app, agent_id, tmp_path)
        requests = []
        entered, release = asyncio.Event(), asyncio.Event()
        replies = [
            chat(calls=[("old", "read_file", {"path": "main.py"})]),
            chat(calls=[("new", "read_file", {"path": "main.py"})]),
            chat("Checked again."),
        ]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            original = runtime.executor.execute

            async def held(task, call):
                result = await original(task, call)
                if call["id"] == "old":
                    entered.set()
                    await release.wait()
                return result

            runtime.executor.execute = held
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Original objective",
                        "request_id": "steer",
                        "workspace": str(project),
                    }
                )
                await asyncio.wait_for(entered.wait(), 2)
                steering = asyncio.create_task(runtime.steer(tid, "Read it again before delivery."))
                await asyncio.sleep(0.02)
                assert not steering.done() and len(requests) == 1
                release.set()
                await asyncio.wait_for(steering, 2)
                assert (await settle(runtime, tid))["status"] == "completed"
                assert len(requests) == 3
                transcript = requests[1]["messages"]
                assert transcript[-2]["tool_call_id"] == "old"
                assert json.loads(transcript[-1]["content"]) == {
                    "owner_instruction": "Read it again before delivery."
                }
                assert runtime.get(tid)["objective"] == "Original objective"
                assert "owner_instruction" not in runtime.store.load(tid)
                assert (
                    len([e for e in runtime.events(tid) if e["type"] == "owner_instruction"]) == 1
                )
            finally:
                release.set()
                await runtime.close()

    asyncio.run(run())


def test_steering_at_step_limit_remains_paused_then_survives_restart(app, agent_id, tmp_path):
    async def run():
        configure(app)
        project = project_for(app, agent_id, tmp_path)
        requests = []
        async with client_for(
            [
                chat(calls=[("old", "read_file", {"path": "main.py"})]),
                chat("Awaiting a fresh check."),
            ],
            requests,
        ) as client:
            runtime = TaskRuntime(app, client, max_steps=1)
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Check",
                        "request_id": "steer-limit",
                        "workspace": str(project),
                    }
                )
                assert (await settle(runtime, tid))["status"] == "paused"
                task = await runtime.steer(tid, "Reconsider the result.")
                assert (
                    task["status"] == "paused"
                    and task["owner_instruction"] == "Reconsider the result."
                )
                assert not runtime.store.load(tid)["verified"]
                assert len(requests) == 1
                await runtime.close()
                runtime = TaskRuntime(app, client)
                await runtime.start()
                await runtime.resume(tid, additional_steps=1)
                assert (await settle(runtime, tid))["status"] == "needs_review"
                assert (
                    json.loads(requests[1]["messages"][-1]["content"])["owner_instruction"]
                    == "Reconsider the result."
                )
            finally:
                await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("text", ["", " ", "x" * 32_001, None, 4])
def test_steering_rejects_invalid_text_without_mutating_task(app, agent_id, text):
    async def run():
        configure(app)
        async with client_for([], []) as client:
            runtime = TaskRuntime(app, client, foreground_busy=lambda: True)
            try:
                tid = await runtime.submit(
                    {"agent_id": agent_id, "objective": "Check", "request_id": "invalid-steer"}
                )
                before = runtime.get(tid)
                with pytest.raises(ValueError):
                    await runtime.steer(tid, text)
                assert runtime.get(tid) == before
                assert not runtime.store.load(tid).get("owner_instruction")
            finally:
                await runtime.close()

    asyncio.run(run())


def test_steering_with_unknown_dependencies_keeps_instruction_paused(
    app, agent_id, tmp_path, boundary
):
    async def run():
        configure(app)
        project = project_for(app, agent_id, tmp_path)
        candidate = {"manager": "pip", "packages": ["requests==2.32.3"]}
        requests = []
        async with client_for(
            [chat(calls=[("deps", "request_dependencies", candidate)])], requests
        ) as client:
            runtime = TaskRuntime(app, client, docker_image="fixture:approved")
            service = app.dependencies = DependencyService(app, runtime)
            try:
                tid = await runtime.submit(
                    {
                        "agent_id": agent_id,
                        "objective": "Check",
                        "request_id": "steer-fence",
                        "workspace": str(project),
                    }
                )
                assert (await settle(runtime, tid))["status"] == "blocked"
                plan = service.prepare(tid, **candidate, request_id="owner")
                service.store.result(plan["id"], "unknown", error="process_interrupted")
                task = await runtime.steer(tid, "Wait for my reconciliation.")
                assert task["status"] == "paused"
                assert task["owner_instruction"] == "Wait for my reconciliation."
                assert len(requests) == 1 and runtime.store.load(tid)["dependencies_required"]
            finally:
                await runtime.close()
                await service.aclose()

    asyncio.run(run())


def test_owner_steering_does_not_rebase_changed_self_or_rebuild_task(app, agent_id):
    async def run():
        configure(app)
        requests = []
        async with client_for([], requests) as client:
            runtime = TaskRuntime(app, client, foreground_busy=lambda: True)
            try:
                tid = await runtime.submit(
                    {"agent_id": agent_id, "objective": "Keep this task", "request_id": "self"}
                )
                await runtime.pause(tid)
                ref = app.history.add(
                    agent_id,
                    "new identity evidence",
                    kind="user",
                    scope="real",
                    request_id="identity",
                )
                proposal = app.identity.propose(
                    agent_id,
                    kind="identity",
                    key="identity.name",
                    text="New name",
                    sources=[ref],
                    base_revision=0,
                    request_id="changed",
                )
                app.identity.decide(proposal["id"], "approve", expected_revision=0)
                runtime.foreground_busy = lambda: False
                await runtime.steer(tid, "Continue this task.")
                task = await settle(runtime, tid)
                assert task["status"] == "blocked" and task["error_code"] == "self_changed"
                assert runtime.store.load(tid)["self_revision"] == 0
                assert task["owner_instruction"] == "Continue this task."
                await runtime.resume(tid)
                assert (await settle(runtime, tid))["error_code"] == "self_changed"
                assert requests == [] and [t["id"] for t in runtime.list()] == [tid]
            finally:
                await runtime.close()

    asyncio.run(run())
