import { chromium, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

// Explicit test fixtures, intercepted only at the HTTP boundary. No live credentials or model calls.
const output = new URL("../.qa/", import.meta.url);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const agent = { id: "test-agent", name: "灵" };
  const ref = { id: "test-source", revision: 2 };
  const episode = { id: "test-episode", title: "测试经历：整理书桌", summary: "用于验证记忆浏览与来源追溯的测试资料。", scope: "real", sources: [ref] };
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    expect(request.headers().authorization).toBe("Bearer test-browser-session");
    const path = new URL(request.url()).pathname;
    let body;
    if (path === "/api/agents") body = [agent];
    else if (path.endsWith("/state")) body = { agent_id: agent.id, revision: 0, entries: {} };
    else if (path.endsWith("/proposals")) body = [{ id: "test-proposal", key: "identity.description", text: "测试候选：耐心倾听，再给出建议。", status: "awaiting_owner", sources: [ref], base_revision: 0 }];
    else if (path.endsWith("/revisions")) body = [{ revision: 0, entries: {}, reason: "initialize" }];
    else if (path.endsWith("/episodes")) body = [episode];
    else if (path === "/api/episodes/test-episode") body = episode;
    else if (path === "/api/sources/test-source") { expect(new URL(request.url()).searchParams.get("revision")).toBe("2"); body = { ...ref, text: "测试原文：先整理书，再整理其他东西。", kind: "user", scope: "real" }; }
    else if (path === "/api/models") body = [];
    else if (path === "/api/models/selection") body = { primary: null, fallback: null };
    else throw new Error("Unexpected test request " + path);
    await route.fulfill({ json: body });
  });
  await page.goto("http://127.0.0.1:5173/#token=test-browser-session");
  await expect(page.getByRole("heading", { name: "灵的空间" })).toBeVisible();
  await expect(page.getByText("等待你的审阅", { exact: true })).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  await page.screenshot({ path: fileURLToPath(new URL("overview-desktop.png", output)), fullPage: true });
  await page.getByRole("button", { name: "记忆", exact: true }).click();
  await page.getByRole("button", { name: episode.title, exact: true }).click();
  await page.getByRole("button", { name: "查看来源 1，版本 2" }).click();
  await expect(page.getByText("测试原文：先整理书，再整理其他东西。")).toBeVisible();
  await page.getByRole("button", { name: "身份审阅", exact: true }).click();
  await expect(page.getByRole("button", { name: "批准这项变化" })).toBeEnabled();
  await page.screenshot({ path: fileURLToPath(new URL("identity-desktop.png", output)), fullPage: true });
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    for (const name of ["概览", "身份审阅", "记忆", "设置"]) {
      await page.getByRole("button", { name, exact: true }).click();
      if (name === "设置") await expect(page.getByLabel("连接名称")).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      expect(overflow, name + " must fit " + width + "px").toBe(false);
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "概览", exact: true }).click();
  await page.screenshot({ path: fileURLToPath(new URL("overview-mobile.png", output)), fullPage: true });
  expect(errors).toEqual([]);
  console.log("Browser smoke passed: desktop, source drilldown, token removal, 390px and 320px layouts; no page errors.");
} finally { await browser.close(); }
