"""Live UI smoke; requires an explicitly configured model in the disposable local service."""

import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe"
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8765/#token=temporary-ui-verification-token")
        page.get_by_role("button", name="对话", exact=True).click()
        page.get_by_label("消息", exact=True).fill("这是一条图形界面测试，请回复：界面已连接。")
        page.get_by_role("button", name="发送消息", exact=True).click()
        expect(page.get_by_text("回复完成", exact=True)).to_be_visible(timeout=60000)
        expect(page.get_by_label("本次对话")).to_contain_text("界面已连接")
        assert "token=" not in page.url
        Path(".artifacts").mkdir(exist_ok=True)
        page.screenshot(path=".artifacts/chat-live.png", full_page=True)
        page.reload()
        page.get_by_role("button", name="对话", exact=True).click()
        expect(page.get_by_text("回复完成", exact=True)).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    assert not errors
    print(json.dumps({"live_model_chat": True, "reload_preserved": True, "page_errors": 0}))


if __name__ == "__main__":
    main()
