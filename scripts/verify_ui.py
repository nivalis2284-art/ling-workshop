"""Real-browser smoke against a running disposable local verification server."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

BASE = "http://127.0.0.1:8765"
TOKEN = "temporary-ui-verification-token"  # A synthetic local-owner token, never a model key.


def main():
    request = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + TOKEN})
    health = request.get("/health")
    health.raise_for_status()
    agents = request.get("/api/agents").json()
    agent = (
        agents[0]["id"]
        if agents
        else request.post("/api/agents", json={"name": "灵·界面验证"}).json()["id"]
    )
    state = request.get(f"/api/agents/{agent}/state").json()
    suffix = uuid4().hex
    ref = request.post(
        "/api/sources",
        json={
            "agent_id": agent,
            "content": "界面验证原文：本周五15:30复查。",
            "request_id": suffix,
        },
    ).json()
    proposal = request.post(
        "/api/proposals",
        json={
            "agent_id": agent,
            "kind": "identity",
            "key": "identity.description",
            "text": "我会先核对依据再形成理解。",
            "sources": [ref],
            "base_revision": state["revision"],
            "request_id": suffix,
        },
    ).json()
    request.post(
        "/api/episodes",
        json={
            "agent_id": agent,
            "title": "界面验证的复查约定",
            "summary": "记录了复查时间，可回到原文核对。",
            "sources": [ref],
            "request_id": suffix,
        },
    ).raise_for_status()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe"
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(BASE + "/#token=" + TOKEN)
        page.get_by_role("button", name="身份审阅", exact=True).click()
        expect(page.get_by_role("heading", name="身份与变化", exact=True)).to_be_visible()
        page.get_by_role("button", name="批准这项变化", exact=True).last.click()
        expect(
            page.get_by_label("当前身份").get_by_text("我会先核对依据再形成理解。", exact=True)
        ).to_be_visible()
        assert request.get(f"/api/agents/{agent}/state").json()["revision"] == state["revision"] + 1
        assert "token=" not in page.url
        artifacts = Path(".artifacts")
        artifacts.mkdir(exist_ok=True)
        page.screenshot(path=str(artifacts / "identity.png"), full_page=True)
        page.get_by_role("button", name="记忆", exact=True).click()
        page.get_by_role("heading", name="界面验证的复查约定").first.click()
        page.get_by_role("button", name="查看来源 1").click()
        expect(page.get_by_text("界面验证原文：本周五15:30复查。", exact=True)).to_be_visible()
        page.screenshot(path=str(artifacts / "source.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    request.close()
    assert not errors, errors
    print(
        json.dumps(
            {
                "browser": "Chrome",
                "real_backend": True,
                "approved_proposal": proposal["id"],
                "source_detail": True,
                "mobile_no_overflow": True,
                "page_errors": len(errors),
            }
        )
    )


if __name__ == "__main__":
    main()
