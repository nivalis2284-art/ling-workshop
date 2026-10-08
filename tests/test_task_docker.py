"""Synthetic live Docker task checks, opt in with LINGNEXUS_TASK_DOCKER_IMAGE."""
import asyncio
import json
import os

import httpx
import pytest
from test_tasks import chat, configure, settle


def test_candidate_sources_are_attached_from_precise_task_evidence(app, agent_id):
    async def run():
        from test_tasks import client_for

        from lingnexus.tasks import TaskRuntime
        configure(app)
        source = app.history.add(agent_id, "abcdef source", kind="user", scope="real", request_id="precise")
        requests = []
        candidate = {"name": "Procedure", "description": "Check original slices", "instructions": "Read precise source before inferring."}
        replies = [chat(calls=[("source", "read_source", {"source_id": source.id, "revision": source.revision, "offset": 2, "limit": 3})]), chat(calls=[("save", "save_skill", candidate)]), chat("Candidate prepared, awaiting validation.")]
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client)
            tid = await runtime.submit({"agent_id": agent_id, "objective": "Learn a reusable procedure as a candidate", "request_id": "candidate"})
            state = await settle(runtime, tid)
            assert state["status"] == "completed", state
            assert '"text": "cde"' in requests[1]["messages"][-1]["content"]
            methods = app.skills.list(agent_id)
            assert len(methods) == 1 and methods[0]["status"] == "candidate"
            assert app.skills.available(agent_id) == []
            proof = next(e["data"]["result"]["source"] for e in runtime.events(tid) if e["type"] == "tool_result" and e["data"]["name"] == "read_source")
            assert methods[0]["sources"] == [proof, source.model_dump()]
            schema = next(t["function"]["parameters"] for t in requests[0]["tools"] if t["function"]["name"] == "save_skill")
            assert set(schema["properties"]) == {"name", "description", "instructions"}
            app.history.correct(source, "Evidence corrected")
            assert app.skills.read(methods[0]["id"])["status"] == "stale"
            await runtime.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("LINGNEXUS_TASK_DOCKER_IMAGE"), reason="local image opt-in")
def test_real_docker_failure_fix_verify_and_review(app, agent_id, tmp_path):
    async def run():
        from lingnexus.tasks import TaskRuntime
        configure(app)
        work = tmp_path / "docker-project"
        work.mkdir()
        (work / "main.py").write_text("VALUE = 0\n")
        (work / "test_main.py").write_text(
            "import unittest, tempfile, os\nfrom main import VALUE\n"
            "class Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 42)\n"
            " def test_isolation(self):\n  self.assertNotEqual(os.getuid(), 0)\n"
            "  self.assertEqual(os.environ['TMPDIR'], '/workspace/.tmp')\n"
            "  self.assertEqual(tempfile.gettempdir(), '/workspace/.tmp')\n"
            "  with tempfile.TemporaryFile() as f: f.write(b'tmp works')\n")
        app.permissions.configure(agent_id, readable=[work], writable=[work], expected_revision=0)
        argv = ["python", "-B", "-m", "unittest", "-v"]
        replies = [chat("Plan: test, fix, retest.", [("test1", "run_command", {"argv": argv, "purpose": "verify"})]),
                   chat(calls=[("fix", "write_file", {"path": "main.py", "text": "VALUE = 42\n"})]),
                   chat(calls=[("test2", "run_command", {"argv": argv, "purpose": "verify"})]),
                   chat(calls=[("candidate", "save_skill", {"name": "Fix and verify", "description": "Test before and after editing", "instructions": "Run the test, fix the failing value, rerun the same test."})]), chat("All tests passed; deliver the patch and pending candidate.")]
        def handler(request):
            body = json.loads(request.content)
            assert "approve" not in {tool["function"]["name"] for tool in body["tools"]}
            assert all("approve" not in tool["function"]["parameters"].get("properties", {}) for tool in body["tools"])
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=replies.pop(0))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = TaskRuntime(app, client, docker_image=os.environ["LINGNEXUS_TASK_DOCKER_IMAGE"])
            tid = await runtime.submit({"agent_id": agent_id, "objective": "Fix project and run tests",
                                        "request_id": "docker-live", "workspace": str(work)})
            result = await settle(runtime, tid)
            assert result["status"] == "completed" and result["verified"], result
            runs = [e["data"]["result"] for e in runtime.events(tid) if e["type"] == "tool_result" and e["data"]["name"] == "run_command"]
            assert len(runs) == 2
            assert runs[0]["exit_code"] != 0 and runs[0]["status"] == "failed"
            assert runs[1]["exit_code"] == 0 and runs[1]["status"] == "success"
            assert runs[1]["verification_passed"] is True
            assert all(r["cleanup_confirmed"] and not r["timed_out"] for r in runs)
            assert "OK" in runs[1]["stderr"]
            methods = app.skills.list(agent_id)
            assert len(methods) == 1 and methods[0]["status"] == "candidate" and methods[0]["sources"]
            assert not app.skills.available(agent_id)
            with app.database.transaction() as db:
                for ref in methods[0]["sources"]:
                    assert db.execute("SELECT 1 FROM task_steps WHERE task_id=? AND result LIKE ?", (tid, '%' + ref["id"] + '%')).fetchone()
            assert (work / "main.py").read_text() == "VALUE = 0\n"
            changes = runtime.changes(tid)
            await runtime.apply(tid, review_id=changes["review_id"])
            assert (work / "main.py").read_text() == "VALUE = 42\n"
            await runtime.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("LINGNEXUS_TASK_DOCKER_IMAGE"), reason="local image opt-in")
@pytest.mark.parametrize("control", ["pause", "cancel"])
def test_control_during_live_command_commits_result_without_replay(app, agent_id, tmp_path, control):
    async def run():
        from test_tasks import client_for

        from lingnexus.tasks import TaskRuntime
        configure(app)
        work = tmp_path / "control-project"
        work.mkdir()
        app.permissions.configure(agent_id, readable=[work], writable=[work], expected_revision=0)
        code = "from pathlib import Path; import time; Path('begun').write_text('once'); time.sleep(2); print('PASS')"
        replies = [chat(calls=[("check", "run_command", {"argv": ["python", "-B", "-c", code], "purpose": "verify"})]), chat("Verified")]
        requests = []
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client, docker_image=os.environ["LINGNEXUS_TASK_DOCKER_IMAGE"])
            tid = await runtime.submit({"agent_id": agent_id, "objective": "Run check once", "request_id": control, "workspace": str(work)})
            for _ in range(150):
                if (runtime.workspace(tid) / "begun").exists():
                    break
                await asyncio.sleep(.05)
            else:
                pytest.fail("container did not begin the synthetic command")
            await getattr(runtime, control)(tid)
            assert runtime.get(tid)["status"] == ("paused" if control == "pause" else "cancelled")
            command = next(e["data"]["result"] for e in runtime.events(tid) if e["type"] == "tool_result")
            assert command["exit_code"] == 0 and command["cleanup_confirmed"] and command["verification_passed"]
            assert len(requests) == 1
            await runtime.close()
            if control == "pause":
                recovered = TaskRuntime(app, client)
                await recovered.start()
                await recovered.resume(tid)
                assert (await settle(recovered, tid))["status"] == "completed"
                assert len(requests) == 2
                assert len([e for e in recovered.events(tid) if e["type"] == "tool_started"]) == 1
                await recovered.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("LINGNEXUS_TASK_DOCKER_IMAGE"), reason="local image opt-in")
@pytest.mark.parametrize("mode", ["failed", "stale", "truncated", "oversized", "timeout"])
def test_model_cannot_override_failed_stale_or_truncated_test_evidence(app, agent_id, tmp_path, mode):
    async def run():
        from lingnexus.tasks import TaskRuntime
        from lingnexus.tools import ExecutionLimits
        configure(app)
        work = tmp_path / "test-evidence"
        work.mkdir()
        (work / "main.py").write_text("VALUE = 1\n")
        app.permissions.configure(agent_id, readable=[work], writable=[work], expected_revision=0)
        code = {"failed": "import sys; print('FAIL'); sys.exit(7)", "stale": "import main; assert main.VALUE == 1; print('PASS')", "truncated": "print('x' * 100000)", "oversized": "from pathlib import Path; Path('large.bin').write_bytes(b'x' * 1048577); print('created')", "timeout": "import time; print('started', flush=True); time.sleep(10)"}[mode]
        replies = [chat(calls=[("test", "run_command", {"argv": ["python", "-B", "-c", code], "purpose": "verify"})])]
        if mode == "stale":
            replies += [chat(calls=[("edit", "write_file", {"path": "main.py", "text": "VALUE = 2\n"})]), chat(calls=[("read", "read_file", {"path": "main.py"})])]
        replies.append(chat("All tests passed and the task is complete!"))
        requests = []
        from test_tasks import client_for
        async with client_for(replies, requests) as client:
            runtime = TaskRuntime(app, client, docker_image=os.environ["LINGNEXUS_TASK_DOCKER_IMAGE"], limits=ExecutionLimits(max_output_bytes=256, timeout_seconds=1 if mode == "timeout" else 30))
            tid = await runtime.submit({"agent_id": agent_id, "objective": "Test the result", "request_id": mode, "workspace": str(work)})
            result = await settle(runtime, tid)
            assert result["status"] == "needs_review" and not result["verified"], result
            command = next(e["data"]["result"] for e in runtime.events(tid) if e["type"] == "tool_result" and e["data"]["name"] == "run_command")
            assert command["cleanup_confirmed"]
            if mode == "failed":
                assert command["exit_code"] == 7 and command["stdout"].strip() == "FAIL"
            elif mode == "truncated":
                assert command["exit_code"] == 0 and command["output_truncated"]
            elif mode == "oversized":
                assert command["exit_code"] == 0 and command["artifact_error"]
                assert command["stdout"].strip() == "created"
            elif mode == "timeout":
                assert command["timed_out"] and command["status"] == "timeout"
            else:
                assert command["exit_code"] == 0 and command["stdout"].strip() == "PASS"
            await runtime.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("LINGNEXUS_TASK_NODE_IMAGE"), reason="local Node image opt-in")
def test_node_environment_uses_selected_image_and_type_stripping(app, agent_id, tmp_path):
    async def run():
        from test_tasks import client_for

        from lingnexus.tasks import TaskRuntime
        configure(app)
        folder = tmp_path / "node-project"
        folder.mkdir()
        (folder / "main.ts").write_text("export const value: number = 0;\n")
        (folder / "check.ts").write_text("import {value} from './main.ts';\nimport assert from 'node:assert/strict';\nimport os from 'node:os';\nassert.equal(os.tmpdir(), '/workspace/.tmp');\nassert.notEqual(process.getuid?.(), 0);\nassert.equal(value, 42);\nconsole.log('node-typescript-check-passed');\n")
        app.permissions.configure(agent_id, readable=[folder], writable=[folder], expected_revision=0)
        argv = ["node", "--disable-warning=ExperimentalWarning", "check.ts"]
        replies = [chat(calls=[("fail", "run_command", {"argv": argv, "purpose": "verify"})]), chat(calls=[("fix", "write_file", {"path": "main.ts", "text": "export const value: number = 42;\n"})]), chat(calls=[("pass", "run_command", {"argv": argv, "purpose": "verify"})]), chat("TypeScript checked; deliver staged patch.")]
        async with client_for(replies, []) as client:
            runtime = TaskRuntime(app, client, docker_image="python:not-installed", node_image=os.environ["LINGNEXUS_TASK_NODE_IMAGE"])
            tid = await runtime.submit({"agent_id": agent_id, "objective": "Fix and verify TypeScript", "request_id": "node-live", "workspace": str(folder), "environment": "node"})
            result = await settle(runtime, tid)
            assert result["status"] == "completed" and result["environment"] == "node", result
            runs = [e["data"]["result"] for e in runtime.events(tid) if e["type"] == "tool_result" and e["data"]["name"] == "run_command"]
            assert runs[0]["exit_code"] != 0 and runs[1]["exit_code"] == 0
            assert runs[1]["verification_passed"] and runs[1]["cleanup_confirmed"]
            assert runs[1]["stdout"].strip() == "node-typescript-check-passed"
            assert (folder / "main.ts").read_text() == "export const value: number = 0;\n"
            assert runtime.changes(tid)["files"][0]["path"] == "main.ts"
            await runtime.aclose()
    asyncio.run(run())
