"""One real consolidation in a disposable database, runtime key from stdin only."""

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from lingnexus.app import Application
from lingnexus.learning import ConsolidationWorker
from lingnexus.models import APIFamily, ModelProfile


async def probe(config):
    with TemporaryDirectory(prefix="lingnexus-learning-") as directory:
        app = Application(Path(directory) / "ling.db")
        agent = app.create_agent("灵·学习验证")
        model = app.profiles.save(
            "学习专用测试",
            ModelProfile(
                api_family=APIFamily.OPENAI_CHAT_COMPLETIONS,
                model=config["model"],
                base_url=config["base_url"],
                max_output_tokens=8192,
                context_window_tokens=32768,
            ),
            api_key=config.pop("api_key"),
        )
        app.profiles.select(model["id"], None)
        app.history.add(
            agent,
            "这是合成测试资料：我偏好回复先给结论，再解释理由。本周五15:30复查，具体日期尚未确定。",
            kind="user",
            scope="real",
            request_id="learning-probe",
        )
        worker = ConsolidationWorker(app)
        try:
            result = await worker.run_once(agent)
            return {
                "worker": result,
                "episodes": app.memory.find_episodes(agent, ""),
                "knowledge": app.memory.knowledge.search(agent, ""),
                "self": app.identity.current(agent).model_dump(),
                "proposals": app.identity.list_proposals(agent),
            }
        finally:
            await worker.aclose()
            app.close()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(asyncio.run(probe(json.load(sys.stdin))), ensure_ascii=True))
    except Exception as error:  # noqa: BLE001 -- sanitize live-test boundary
        print(json.dumps({"error": type(error).__name__}))
        raise SystemExit(1)
