"""Authorized small dependency smoke; no cloud request or credential in this test."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from lingnexus.app import Application
from lingnexus.dependencies import DependencyService
from lingnexus.models import APIFamily, ModelProfile
from lingnexus.tasks import TaskRuntime
from lingnexus.tools import DockerExecutor, ExecutionLimits


async def main():
    with TemporaryDirectory(prefix="lingnexus-dependencies-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        (project / "file.py").write_text("print('fixture')", encoding="utf-8")
        app = Application(root / "data" / "ling.db")
        agent = app.create_agent("依赖测试")
        profile = app.profiles.save(
            "不调用的测试配置",
            ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model="fixture",
                base_url="https://not-called.invalid",
            ),
        )
        app.profiles.select(profile["id"], None)
        app.permissions.configure(
            agent, readable=[project], writable=[project], expected_revision=0
        )
        task = TaskRuntime(app, docker_image="python:3.12-slim")
        dependency = DependencyService(app, task)
        app.dependencies = dependency
        try:
            tid = await task.submit(
                {
                    "agent_id": agent,
                    "objective": "专用依赖配置测试，不请求模型",
                    "request_id": "dependency",
                    "workspace": str(project),
                }
            )
            await task.pause(tid)
            plan = dependency.prepare(
                tid, manager="pip", packages=["colorama==0.4.6"], request_id="approved-plan"
            )
            result = await dependency.approve(plan["id"], intent_hash=plan["intent_hash"])
            verification = DockerExecutor(
                task.workspace(tid),
                image="python:3.12-slim",
                writable=False,
                limits=ExecutionLimits(timeout_seconds=30),
            ).execute(
                [
                    "env",
                    "PYTHONPATH=/workspace/.deps/python",
                    "python",
                    "-B",
                    "-c",
                    "import colorama; print(colorama.__version__)",
                ]
            )
            print(
                json.dumps(
                    {
                        "plan_status": result["status"],
                        "verification": asdict(verification),
                        "project_untouched": not (project / ".deps").exists(),
                    },
                    ensure_ascii=True,
                )
            )
        finally:
            await dependency.aclose()
            await task.aclose()
            app.close()


if __name__ == "__main__":
    asyncio.run(main())
