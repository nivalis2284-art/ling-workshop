"""Exercise packaged GUI/cache with synthetic keys and no model calls."""

import json
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "packaging/dist/LingNexus/LingNexus.exe"


def launch(data):
    process = subprocess.Popen(
        [str(EXE), "--background", "--data-dir", str(data), "--port", "0"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    deadline = time.monotonic() + 30
    session = data / ".desktop/owner-session.json"
    while not session.is_file():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.1)
    value = json.loads(session.read_text("utf-8"))
    base = f"http://127.0.0.1:{value['port']}"
    client = httpx.Client(
        base_url=base,
        headers={"Authorization": "Bearer " + value["token"]},
        trust_env=False,
        timeout=20,
    )
    return process, client, base + "/#token=" + value["token"]


def stop(data, process, client):
    client.close()
    subprocess.run(
        [str(EXE), "--quit", "--data-dir", str(data)],
        check=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert process.wait(timeout=30) == 0


def main():
    with TemporaryDirectory(prefix="credential-ui-", dir=ROOT / ".artifacts") as directory:
        data = Path(directory)
        process, api, url = launch(data)
        errors = []
        try:
            assert api.post("/api/agents", json={"name": "灵·缓存验证"}).is_success
            assert api.put("/api/background", json={"enabled": False}).is_success
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(type(error).__name__))
                page.goto(url)
                page.get_by_role("button", name="设置", exact=True).click()
                for name, family, address, key in [
                    (
                        "提供商甲",
                        "openai_chat_completions",
                        "https://one.example.test",
                        "first-synthetic-key",
                    ),
                    (
                        "提供商乙",
                        "anthropic_messages",
                        "https://two.example.test",
                        "second-synthetic-key",
                    ),
                ]:
                    page.get_by_label("连接名称", exact=True).fill(name)
                    page.get_by_label("接口类型").select_option(family)
                    page.get_by_label("模型名称", exact=True).fill("fixture-model")
                    page.get_by_label("服务地址", exact=True).fill(address)
                    page.get_by_label("模型密钥", exact=True).fill(key)
                    page.get_by_role("button", name="保存模型配置", exact=True).click()
                    expect(page.get_by_role("button", name="编辑连接 " + name)).to_be_visible()
                    expect(page.get_by_label("模型密钥", exact=True)).to_have_value("")
                models = api.get("/api/models").json()
                assert len(models) == 2 and all(m["credentials_available"] for m in models)
                assert api.put(
                    "/api/models/selection",
                    json={"primary": models[0]["id"], "fallback": models[1]["id"]},
                ).is_success
                assert page.locator('link[rel="icon"]').get_attribute("href") == "/favicon.svg"
                favicon = api.get("/favicon.svg")
                assert favicon.is_success and "M5 21h14" in favicon.text
                page.close()
                assert api.get("/health").json()["status"] == "ok"
                browser.close()
            cache = data / "cache/model-credentials.bin"
            assert cache.is_file() and b"synthetic-key" not in cache.read_bytes()
        finally:
            stop(data, process, api)
        process, api, url = launch(data)
        try:
            models = api.get("/api/models").json()
            assert len(models) == 2 and all(m["credentials_available"] for m in models)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(type(error).__name__))
                page.goto(url)
                page.get_by_role("button", name="设置", exact=True).click()
                expect(page.get_by_text("密钥可用", exact=False)).to_have_count(2)
                page.screenshot(
                    path=str(ROOT / ".artifacts/credential-cache-ui.png"), full_page=True
                )
                page.get_by_role("button", name="清除模型密钥缓存", exact=True).click()
                page.get_by_role("button", name="确认清除密钥缓存", exact=True).click()
                expect(page.get_by_text("需要填写密钥", exact=False)).to_have_count(2)
                assert not cache.exists()
                assert not any(m["credentials_available"] for m in api.get("/api/models").json())
                browser.close()
        finally:
            stop(data, process, api)
        process, api, _ = launch(data)
        try:
            models = api.get("/api/models").json()
            assert len(models) == 2 and not any(m["credentials_available"] for m in models)
            selection = api.get("/api/models/selection").json()
            assert (
                selection["primary"] == models[0]["id"] and selection["fallback"] == models[1]["id"]
            )
            assert not errors
            result = {
                "status": "passed",
                "providers": 2,
                "restart_without_reentry": True,
                "encrypted_file": True,
                "clear_disk_and_memory": True,
                "clear_survives_restart": True,
                "profiles_selection_preserved": True,
                "favicon_served": True,
                "page_errors": 0,
                "live_model_calls": 0,
            }
            (ROOT / ".artifacts/credential-ui-result.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            print(json.dumps(result))
        finally:
            stop(data, process, api)


if __name__ == "__main__":
    main()
