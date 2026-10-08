import { expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { consumeOwnerSession, forgetOwnerSession } from "../api/session";
import { App } from "../app/App";

test("启动入口的令牌在首个请求前从地址栏移除，仅在会话存储中保留", async () => {
  window.history.replaceState(
    null,
    "",
    "/?view=home#token=test-only-fragment-token",
  );
  const token = consumeOwnerSession();
  expect(window.location.hash).toBe("");
  expect(window.location.search).toBe("?view=home");
  expect(consumeOwnerSession()).toBe("test-only-fragment-token");
  expect(localStorage.length).toBe(0);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_input: string, init: RequestInit) => {
      expect(window.location.hash).toBe("");
      expect(new Headers(init.headers).get("Authorization")).toBe(
        "Bearer test-only-fragment-token",
      );
      return new Response("[]", { status: 200 });
    }),
  );
  render(<App initialToken={token} />);
  await screen.findByLabelText("灵的名字");
  expect(document.body.textContent).not.toContain("test-only-fragment-token");
});

test("会话失效后清除凭据并回到连接指引，不继续显示本地资料", async () => {
  window.history.replaceState(null, "", "/#token=test-expired-token");
  const token = consumeOwnerSession();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () => new Response('{"detail":"unauthorized"}', { status: 401 }),
    ),
  );
  render(<App initialToken={token} />);
  expect(
    await screen.findByRole("heading", { name: "连接你的本地空间" }),
  ).toBeInTheDocument();
  expect(
    screen.getByText("本次会话已结束，需要重新连接。"),
  ).toBeInTheDocument();
  expect(consumeOwnerSession()).toBeNull();
});

test("空令牌片段或手动断开会清除上一次会话", () => {
  window.history.replaceState(null, "", "/#token=test-first-session");
  consumeOwnerSession();
  window.history.replaceState(null, "", "/#token=");
  expect(consumeOwnerSession()).toBeNull();
  expect(consumeOwnerSession()).toBeNull();
  window.history.replaceState(null, "", "/#token=test-second-session");
  consumeOwnerSession();
  forgetOwnerSession();
  expect(consumeOwnerSession()).toBeNull();
});
