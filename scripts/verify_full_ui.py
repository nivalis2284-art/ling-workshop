"""Real backend/Chrome integration with synthetic evidence and an owned form server."""

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_browser_live import form_server

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".artifacts/ui-full")
    args = parser.parse_args()
    session = json.loads(
        (args.data_dir / ".desktop/owner-session.json").read_text(encoding="utf-8")
    )
    base = f"http://127.0.0.1:{session['port']}"
    client = httpx.Client(
        base_url=base, headers={"Authorization": "Bearer " + session["token"]}, timeout=30
    )
    agent = client.get("/api/agents").json()[0]["id"]
    suffix = uuid4().hex
    ref = client.post(
        "/api/sources",
        json={
            "agent_id": agent,
            "content": "专用界面验证：周五15:30在东楼复查。",
            "request_id": suffix,
        },
    ).json()
    client.post(
        "/api/episodes",
        json={
            "agent_id": agent,
            "title": "GUI复查验证",
            "summary": "安排复查，细节见原文。",
            "sources": [ref],
            "request_id": suffix,
        },
    ).json()
    forms = form_server.__wrapped__()
    origin, posts = next(forms)
    route = f"/api/agents/{agent}/browser"
    grant = client.get(route + "/origins").json()
    revision = client.put(
        route + "/origins",
        json={
            "expected_revision": grant["revision"],
            "grants": [{"origin": origin, "allow_local": True}],
        },
    ).json()["revision"]

    def browser_action(action, **args):
        result = client.post(
            route + "/actions",
            json={"request_id": uuid4().hex, "action": action, "origin_revision": revision, **args},
        )
        result.raise_for_status()
        return result.json()

    browser_action("goto", url=origin + "/form")
    page_state = client.get(route + "/state").json()["page"]
    browser_action(
        "fill",
        page_version=page_state["page_version"],
        selector="#message",
        value="Approved GUI fixture",
    )
    page_state = client.get(route + "/state").json()["page"]
    browser_action("submit", page_version=page_state["page_version"], selector="#send")
    assert not posts
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/#token=" + session["token"])
            for label in [
                "概览",
                "身份审阅",
                "记忆",
                "资料",
                "任务",
                "活动与关注",
                "技能",
                "浏览器",
                "后台工作",
                "设置",
            ]:
                page.get_by_role("button", name=label, exact=True).click()
                page.wait_for_timeout(150)
            page.get_by_role("button", name="记忆", exact=True).click()
            page.get_by_role("button", name="GUI复查验证", exact=True).first.click()
            page.get_by_role("button", name="查看来源 1，版本 1", exact=True).click()
            expect(
                page.get_by_text("专用界面验证：周五15:30在东楼复查。", exact=True)
            ).to_be_visible()
            page.get_by_role("button", name="关闭来源").click()
            page.get_by_role("button", name="更正", exact=True).click()
            page.locator(".memory-detail textarea").fill(
                "更正后的安排：周六复查，原始记录仍可核对。"
            )
            page.get_by_role("button", name="保存更正", exact=True).click()
            expect(
                page.get_by_text("更正后的安排：周六复查，原始记录仍可核对。", exact=True)
            ).to_be_visible()
            page.get_by_role("button", name="浏览器", exact=True).click()
            expect(page.get_by_text("Approved GUI fixture", exact=False).first).to_be_visible()
            page.get_by_role("button", name="批准这次操作", exact=True).click()
            page.get_by_role("button", name="确认批准", exact=True).click()
            expect(page.get_by_text("操作已完成", exact=False).first).to_be_visible(timeout=15000)
            assert len(posts) == 1
            assert posts[0]["message"] == ["Approved GUI fixture"]
            page.get_by_role("button", name="对话", exact=True).click()
            page.get_by_role("button", name="新建对话", exact=True).click()
            page.get_by_label("对话名称").fill("完整GUI附件验证")
            page.get_by_role("button", name="创建对话", exact=True).click()
            page.locator(".chat-attachment-picker summary").click()
            page.get_by_label("上传聊天附件").set_input_files(
                {
                    "name": "budget.txt",
                    "mimeType": "text/plain",
                    "buffer": "GUI附件预算：材料费25元，交通费17元，总额42元。".encode(),
                }
            )
            expect(page.get_by_label("选择 budget.txt")).to_be_checked()
            page.get_by_label("消息", exact=True).fill("根据随附文件，预算总额是多少？只回答金额。")
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.get_by_text("回复完成", exact=True)).to_be_visible(timeout=90000)
            expect(page.get_by_label("本次对话")).to_contain_text("42")
            page.screenshot(path=str(ROOT / ".artifacts/full-chat.png"), full_page=True)
            page.reload()
            page.get_by_role("button", name="对话", exact=True).click()
            expect(page.get_by_label("本次对话")).to_contain_text("42")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            browser.close()
    finally:
        client.close()
        try:
            next(forms)
        except StopIteration:
            pass
    assert not errors, errors
    print(
        json.dumps(
            {
                "navigation": 10,
                "memory_corrected": True,
                "approved_form_posts": len(posts),
                "real_model_attachment": True,
                "reload_history": True,
                "page_errors": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
