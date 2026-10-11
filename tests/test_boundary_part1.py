"""Boundary acceptance for the first part.

Two things are proven here:

1. The standalone Agent system is fully usable while the long-term part is not
   assembled — startup, individual registration, a chat turn with an attachment,
   edit / regenerate / delete / branch switch, a task run, and backup + restore.
2. No module reachable from the composition root imports a long-term module.
"""

import ast
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx

from lingnexus.models import APIFamily, ModelProfile

# --- zero reverse dependency ----------------------------------------------

# The long-term part. These files stay in the repository but must never be
# reached from the assembled first part.
SECOND_PART_MODULES = frozenset(
    {
        "lingnexus.history",
        "lingnexus.memory",
        "lingnexus.identity",
        "lingnexus.learning",
        "lingnexus.skills",
        "lingnexus.development",
        "lingnexus.activities",
        "lingnexus.conversations.segments",
        "lingnexus.context.recall",
    }
)
ASSEMBLY_ROOTS = ("lingnexus.app", "lingnexus.api.app")


def _module_path(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ModuleNotFoundError, ValueError):
        return None
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        return None
    return Path(spec.origin)


def _candidates(node: ast.stmt, module: str, package: str) -> list[str]:
    """Fully-qualified module names a single import statement may reference."""
    names: list[str] = []
    if isinstance(node, ast.Import):
        names.extend(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1]) if node.level <= len(parts) else ""
            prefix = f"{base}.{node.module}" if base and node.module else (node.module or base)
            names.extend(f"{prefix}.{alias.name}" for alias in node.names)
        elif node.module:
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    # Include every dotted prefix so packages (__init__.py) are visited too.
    expanded: list[str] = []
    for name in names:
        parts = name.split(".")
        expanded.extend(".".join(parts[: i + 1]) for i in range(len(parts)))
    return expanded


def _reachable_modules(roots) -> dict[str, Path]:
    reached: dict[str, Path] = {}
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in reached:
            continue
        path = _module_path(name)
        if path is None:
            continue
        reached[name] = path
        is_package = path.name == "__init__.py"
        package = name if is_package else name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in _candidates(node, name, package):
                    if target.startswith("lingnexus") and target not in reached:
                        queue.append(target)
    return reached


def test_no_reachable_module_imports_the_second_part():
    reached = _reachable_modules(ASSEMBLY_ROOTS)
    leaked = sorted(set(reached) & SECOND_PART_MODULES)
    assert leaked == [], f"the first part reaches long-term modules: {leaked}"
    # The walk really did traverse a meaningful part of the package.
    assert "lingnexus.runtime.service" in reached
    assert "lingnexus.tasks.service" in reached
    assert "lingnexus.conversations.service" in reached


def test_second_part_services_are_not_assembled(app):
    for absent in (
        "history",
        "identity",
        "memory",
        "development",
        "segments",
        "skills",
        "activities",
    ):
        assert not hasattr(app, absent), f"Application still assembles {absent!r}"


def test_composition_root_mounts_only_first_part_routes(client):
    paths = set(client.app.openapi()["paths"])
    for removed in (
        "/identity",
        "/history",
        "/development",
        "/skills",
        "/activities",
        "/segments",
        "/memory",
        "/sources",
        "/proposals",
    ):
        assert not any(path.startswith(removed) for path in paths), f"route {removed} leaked"
    for kept in (
        "/agents",
        "/chat/",
        "/conversations",
        "/tasks",
        "/permissions",
        "/system/",
        "/models",
        "/maintenance/",
        "/background",
        "/browser/",
        "/dependencies",
        "/attachments/",
    ):
        assert any(kept in path for path in paths), f"route {kept} missing"


# --- helpers --------------------------------------------------------------


def _configure(app):
    item = app.profiles.save(
        "fixture",
        ModelProfile(
            api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
            model="fixture",
            base_url="https://fixture.test",
            context_window_tokens=32000,
            max_output_tokens=1024,
        ),
        api_key="runtime-secret-only",
    )
    app.profiles.select(item["id"], None)
    return item["id"]


def _sse_text(text):
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def _sse_tool(text=None, calls=()):
    delta = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {
                "index": i,
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (cid, name, args) in enumerate(calls)
        ]
    frame = {
        "id": "chat-native",
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}
        ],
    }
    return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"


def _mock(responses, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=responses.pop(0)
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat(app, responses, request, requests=None):
    from lingnexus.runtime import ChatRuntime
    from lingnexus.runtime.types import ChatRequest

    async def scenario():
        async with _mock(responses, requests) as client:
            runtime = ChatRuntime(app, client=client)
            value = request if isinstance(request, ChatRequest) else ChatRequest(**request)
            run = runtime.submit(value)
            events = [event async for event in runtime.events(run["id"])]
            result = runtime.get_run(run["id"])
            await runtime.aclose()
            return result, events

    return asyncio.run(scenario())


async def _settle(runtime, tid):
    for _ in range(2000):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            return runtime.get(tid)
        await asyncio.sleep(0.01)
    raise AssertionError("task worker did not reach a checkpoint")


# --- the full chain -------------------------------------------------------


def test_first_part_is_fully_usable_without_the_second_part(app_factory, tmp_path):
    app = app_factory(tmp_path / "ling.db")
    try:
        # 1. Startup: the background coordinator drives automatic backups only.
        from lingnexus.background import Supervisor
        from lingnexus.maintenance.automatic import AutomaticBackups

        app.automatic_backups = AutomaticBackups(app)
        supervisor = Supervisor(app, foreground_busy=lambda: False)
        supervisor.automatic_backups = app.automatic_backups
        tick = asyncio.run(supervisor.run_now())
        asyncio.run(supervisor.aclose())
        assert tick["status"] == "ok", tick
        assert tick["backup_created"] is True, tick

        # 2. Create an individual (id + name only).
        agent = app.create_agent("灵")
        assert app.list_agents()[0]["name"] == "灵"
        _configure(app)

        # 3. One chat turn carrying an attachment.
        item = app.attachments.add(agent, "notes.txt", "会议记录：预算1760。".encode())
        requests = []
        result, events = _chat(
            app,
            [_sse_text("收到附件")],
            {
                "agent_id": agent,
                "message": "看附件",
                "request_id": "turn-1",
                "attachment_ids": [item["id"]],
            },
            requests,
        )
        assert result["status"] == "completed"
        assert any(event["event"] == "complete" for event in events)
        assert "预算1760" in json.dumps(requests[0], ensure_ascii=False)
        conversation_id = result["conversation_id"]
        user_message_id = result["user_message_id"]

        # 4. Regenerate the reply for that user message.
        regenerated, regen_events = _chat(
            app,
            [_sse_text("重新生成")],
            {
                "agent_id": agent,
                "message": "看附件",
                "request_id": "turn-2",
                "conversation_id": conversation_id,
                "regenerate_message_id": user_message_id,
                "attachment_ids": [item["id"]],
            },
        )
        assert regenerated["status"] == "completed"
        assert any(event["event"] == "complete" for event in regen_events)
        texts = [row["text"] for row in app.conversations.messages(conversation_id)]
        assert texts == ["看附件", "重新生成"], texts

        # 5. Edit the user message: this forks a fresh branch.
        edited = app.conversations.edit_user(
            conversation_id, user_message_id, "看附件（改）", request_id="edit-1"
        )
        assert edited["id"] != user_message_id
        assert [row["text"] for row in app.conversations.messages(conversation_id)] == [
            "看附件（改）"
        ]
        # Give the edited branch a reply, then delete the whole round.
        app.conversations.append_assistant(
            conversation_id, "改后回答", request_id="edit-1-a", expected_head=edited["id"]
        )
        app.conversations.delete_turns(conversation_id, [edited["id"]])
        assert app.conversations.messages(conversation_id) == []

        # 6. Branch switch back to the regenerated branch.
        branch_tip = next(
            branch
            for branch in app.conversations.branches(conversation_id)
            if branch["text"] == "重新生成"
        )
        app.conversations.select_head(conversation_id, branch_tip["id"])
        assert [row["text"] for row in app.conversations.messages(conversation_id)] == [
            "看附件",
            "重新生成",
        ]

        # 7. Build and execute a task (default sandbox tier).
        folder = tmp_path / "authorized"
        folder.mkdir()
        target = folder / "answer.txt"
        app.permissions.configure(
            agent, readable=[folder], writable=[folder], expected_revision=0
        )

        async def run_task():
            from lingnexus.tasks import TaskRuntime

            replies = [
                _sse_tool(
                    "Plan.",
                    [
                        (
                            "write-id",
                            "create_file",
                            {"path": str(target), "text": "artifact"},
                        )
                    ],
                ),
                _sse_tool(calls=[("check-id", "read_file", {"path": str(target)})]),
                _sse_tool("Done."),
            ]
            async with _mock(replies) as client:
                runtime = TaskRuntime(app, client)
                tid = await runtime.submit(
                    {"agent_id": agent, "objective": "create and verify", "request_id": "task-1"}
                )
                state = await _settle(runtime, tid)
                await runtime.close()
                return state

        state = asyncio.run(run_task())
        assert state["status"] == "completed", state
        assert state["verified"] is True
        assert target.read_text() == "artifact"

        # 8. Backup (portable export) and restore into a fresh database.
        export_dir = tmp_path / "export"
        app.maintenance.export_to(export_dir)
        restored = tmp_path / "restored.db"
        app.maintenance.restore_from(export_dir, restored)
    finally:
        app.close()

    other = app_factory(restored)
    try:
        assert [row["name"] for row in other.list_agents()] == ["灵"]
        assert other.attachments.read(item["id"])["original_name"] == "notes.txt"
        assert [row["text"] for row in other.conversations.messages(conversation_id)] == [
            "看附件",
            "重新生成",
        ]
    finally:
        other.close()
