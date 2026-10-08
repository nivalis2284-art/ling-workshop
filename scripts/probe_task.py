"""Controlled real model -> staged project -> Docker test -> delivery probe."""

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from lingnexus.app import Application
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.tasks import TaskRuntime


async def probe(config):
    with TemporaryDirectory(prefix="lingnexus-task-probe-") as directory:
        base = Path(directory)
        workspace = base / "project"
        workspace.mkdir()
        (workspace / "calculator.py").write_text(
            "def add(a,b):\n    return a-b\n", encoding="utf-8"
        )
        (workspace / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\nclass TestCalculator(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(17,25),42)\nif __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )
        app = Application(base / "data" / "ling.db")
        agent = app.create_agent("灵·编程验证")
        model = app.profiles.save(
            "测试模型",
            ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model=config["model"],
                base_url=config["base_url"],
                max_output_tokens=8192,
                context_window_tokens=64000,
            ),
            api_key=config.pop("api_key"),
        )
        app.profiles.select(model["id"], None)
        app.permissions.configure(
            agent, readable=[workspace], writable=[workspace], expected_revision=0
        )
        runtime = TaskRuntime(app, docker_image="python:3.12-slim", max_steps=15)
        await runtime.start()
        try:
            tid = await runtime.submit(
                {
                    "agent_id": agent,
                    "objective": "修复 calculator.py 的 add 加法错误。先运行已有 unittest 观察失败，阅读代码，修改后必须再运行 python -m unittest -v 验证。不要安装依赖。完成后报告具体结果。",
                    "request_id": "coding-smoke",
                    "workspace": str(workspace),
                }
            )
            for _ in range(240):
                task = runtime.get(tid)
                if task["status"] not in {"running", "queued"}:
                    break
                await asyncio.sleep(1)
            task = runtime.get(tid)
            events = runtime.events(tid, limit=200)
            results = [e["data"] for e in events if e["type"] == "tool_result"]
            changes = runtime.changes(tid)
            return {
                "task": {
                    k: task[k]
                    for k in ("id", "status", "verified", "step_count", "final", "error_code")
                },
                "tool_results": results,
                "changes": changes,
                "original_unchanged": (workspace / "calculator.py").read_text(encoding="utf-8")
                == "def add(a,b):\n    return a-b\n",
            }
        finally:
            await runtime.aclose()
            app.close()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(asyncio.run(probe(json.load(sys.stdin))), ensure_ascii=True))
    except Exception as error:  # noqa: BLE001 -- no raw credential-bearing exception text
        print(json.dumps({"failed": type(error).__name__}))
        raise SystemExit(1)
