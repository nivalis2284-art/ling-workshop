"""Real GUI -> cloud Agent -> isolated tests -> owner reviewed file delivery.

Use a dedicated already configured desktop instance; never reads model keys.
"""

import argparse
import json
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".artifacts/ui-final")
    args = parser.parse_args()
    session = json.loads((args.data_dir / ".desktop/owner-session.json").read_text("utf-8"))
    base = f"http://127.0.0.1:{session['port']}"
    project = ROOT / ".artifacts/ui-project"
    original = (project / "calculator.py").read_bytes()
    errors = []
    with httpx.Client(
        base_url=base, headers={"Authorization": "Bearer " + session["token"]}
    ) as api:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            page.goto(base + "/#token=" + session["token"])
            page.get_by_role("button", name="任务", exact=True).click()
            page.get_by_label("任务目标", exact=True).fill(
                "修复 calculator.py 的 add 加法错误。必须先运行 python -B -m unittest -v "
                "观察实际失败，再阅读代码并修改副本，最后再运行同一命令验证成功。"
                "不要安装依赖，不生成额外文件，报告实际测试结果。"
            )
            page.get_by_label("项目文件夹（可选）", exact=True).fill(str(project))
            with page.expect_response(
                lambda response: (
                    response.url == base + "/api/tasks" and response.request.method == "POST"
                )
            ) as submitted:
                page.get_by_role("button", name="创建任务", exact=True).click()
            task_id = submitted.value.json()["id"]
            details = page.get_by_role("region", name="任务详情", exact=True)
            details.get_by_role("button", name="暂停任务", exact=True).click()
            expect(details.get_by_role("button", name="恢复任务", exact=True)).to_be_visible()
            paused = api.get("/api/tasks/" + task_id).json()
            assert paused["status"] == "paused"
            details.get_by_role("button", name="恢复任务", exact=True).click()
            expect(details.get_by_text("已验证结果", exact=True)).to_be_visible(timeout=240_000)
            result = api.get("/api/tasks/" + task_id).json()
            assert result["status"] == "completed" and result["verified"]
            events = api.get("/api/tasks/" + task_id + "/events", params={"limit": 1000}).json()
            commands = [
                e["data"]["result"]
                for e in events
                if e["type"] == "tool_result" and e["data"].get("name") == "run_command"
            ]
            assert any(c.get("exit_code") == 1 for c in commands), "No observed failing test"
            assert commands[-1]["exit_code"] == 0 and commands[-1]["cleanup_confirmed"]
            assert (project / "calculator.py").read_bytes() == original
            details.get_by_role("button", name="审阅文件改动", exact=True).click()
            expect(details.get_by_text("已读取的文件改动", exact=True)).to_be_visible()
            with page.expect_download() as download:
                details.get_by_role("button", name="下载成果：calculator.py", exact=True).click()
            delivered = ROOT / ".artifacts/ui-delivered-calculator.py"
            download.value.save_as(delivered)
            assert b"a+b" in delivered.read_bytes().replace(b" ", b"")
            details.get_by_role("button", name="应用这些改动", exact=True).click()
            page.get_by_role("button", name="确认写回项目", exact=True).click()
            expect(details.get_by_text("改动已写回项目。", exact=True)).to_be_visible()
            assert (project / "calculator.py").read_bytes() == delivered.read_bytes()
            page.screenshot(path=str(ROOT / ".artifacts/task-delivery.png"), full_page=True)
            browser.close()
        assert not errors, errors
        summary = {
            "status": "passed",
            "paused_resumed": True,
            "task_id": task_id,
            "verified": True,
            "observed_exit_codes": [c["exit_code"] for c in commands],
            "original_unchanged_until_approval": True,
            "download_matches_applied": True,
            "page_errors": errors,
        }
        (ROOT / ".artifacts/task-ui-result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    main()
