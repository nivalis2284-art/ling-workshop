import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { BrowserPage } from "./BrowserPage";
import { agent, approval, backend } from "../tasks/test/fixtures";

test("浏览器显示真实意图和表单，批准发送展示的 hash，结果不虚报成功", async () => {
  let pending = [approval];
  const origins = {
    agent_id: agent.id,
    revision: 2,
    grants: [{ origin: "https://example.com", allow_local: false }],
  };
  const { api, requests } = backend(({ path }) => {
    if (path.endsWith("/browser/origins")) return origins;
    if (path.endsWith("/browser/state"))
      return {
        agent_id: agent.id,
        origins,
        page: {
          url: "https://example.com/report",
          page_version: "v1",
          session_id: "session-1",
          manual_login: false,
        },
      };
    if (path.endsWith("/browser/approvals")) return pending;
    if (path.endsWith("/ap1/decision")) {
      pending = [];
      return {
        id: "ba1",
        status: "unknown",
        error_code: "effect_needs_review",
        result: null,
      };
    }
  });
  const user = userEvent.setup();
  render(<BrowserPage api={api} agent={agent} />);
  await screen.findByText("提交周报");
  expect(screen.getAllByText(/本周完成说明/)[0]).toBeVisible();
  expect(screen.getByText(approval.intent_hash)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "批准这次操作" }));
  expect(requests.some((r) => r.method === "POST")).toBe(false);
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认批准",
    }),
  );
  await screen.findByText(/操作结果不确定/);
  expect(requests.find((r) => r.method === "POST")?.body).toEqual({
    decision: "approve",
    expected_intent_hash: "a".repeat(64),
  });
});

test("本地项目来源需勾选 allow_local；手动登录打开并显式结束", async () => {
  let origins = {
    agent_id: agent.id,
    revision: 0,
    grants: [] as { origin: string; allow_local: boolean }[],
  };
  let manual = false;
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.endsWith("/browser/origins")) {
      if (method === "PUT")
        origins = {
          agent_id: agent.id,
          revision: 1,
          grants: body.grants as typeof origins.grants,
        };
      return origins;
    }
    if (path.endsWith("/browser/state"))
      return {
        agent_id: agent.id,
        origins,
        page: manual ? { manual_login: true, session_id: "manual" } : null,
      };
    if (path.endsWith("/browser/approvals")) return [];
    if (path.endsWith("/browser/login")) {
      manual = method === "POST";
      return { manual_login: manual, page: null };
    }
  });
  const user = userEvent.setup();
  render(<BrowserPage api={api} agent={agent} />);
  await user.type(
    await screen.findByLabelText("网站来源"),
    "http://localhost:3000",
  );
  await user.click(screen.getByLabelText("允许访问我自己的本地项目"));
  await user.click(screen.getByRole("button", { name: "添加网站授权" }));
  await waitFor(() =>
    expect(requests.find((r) => r.method === "PUT")?.body).toEqual({
      expected_revision: 0,
      grants: [{ origin: "http://localhost:3000", allow_local: true }],
    }),
  );
  await user.type(
    screen.getByLabelText("登录页面地址"),
    "http://localhost:3000/login",
  );
  await user.click(screen.getByRole("button", { name: "打开浏览器手动登录" }));
  await user.click(await screen.findByRole("button", { name: "我已完成登录" }));
  await waitFor(() =>
    expect(requests.some((r) => r.method === "DELETE")).toBe(true),
  );
  expect(requests.find((r) => r.method === "POST")?.body).toEqual({
    url: "http://localhost:3000/login",
    origin_revision: 1,
    headed: true,
  });
});
