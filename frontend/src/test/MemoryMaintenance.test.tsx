import { expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../api/client";
import { MemoryPage } from "../features/memory/MemoryPage";

const token = "memory-owner-test";
const agent = { id: "agent-private", name: "灵" };
const source = { id: "source-private", revision: 1 };
const episode = {
  id: "episode-private",
  agent_id: agent.id,
  title: "一起整理书桌",
  summary: "先收好所有书。",
  scope: "real",
  revision: 1,
  status: "active",
  sources: [source],
  provenance: "derived",
  corrections: [],
  source_valid: true,
};
const knowledge = {
  ...episode,
  id: "knowledge-private",
  title: undefined,
  summary: undefined,
  statement: "更喜欢安静阅读。",
  kind: "preference",
  scope: "external",
};

function backend(
  options: {
    conflict?: boolean;
    invalid?: boolean;
    failure?: boolean;
    waitMutation?: Promise<void>;
  } = {},
) {
  let items: Record<string, unknown>[] = [episode, knowledge];
  const requests: {
    url: URL;
    method: string;
    body: Record<string, unknown> | null;
  }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      expect(new Headers(init.headers).get("Authorization")).toBe(
        "Bearer " + token,
      );
      expect(init.credentials).toBe("omit");
      const url = new URL(input, "http://localhost");
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : null;
      requests.push({ url, method, body });
      const json = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/memory/agent-private/items") {
        const kind = url.searchParams.get("kind");
        const status = url.searchParams.get("status");
        return json(
          items
            .filter(
              (item) =>
                (kind === "knowledge" ? !!item.statement : !!item.summary) &&
                (status === "all" || status === item.status),
            )
            .map((item) =>
              options.invalid
                ? {
                    ...item,
                    source_valid: false,
                    status: "invalidated",
                    source_notice: "来源已失效，需要重新整理。",
                  }
                : item,
            ),
        );
      }
      const item = items.find((value) =>
        url.pathname.includes(String(value.id)),
      );
      if (item && method === "GET")
        return json(
          options.invalid
            ? {
                ...item,
                source_valid: false,
                status: "invalidated",
                source_notice: "来源已失效，需要重新整理。",
              }
            : item,
        );
      if (item && (method === "PATCH" || method === "POST")) {
        await options.waitMutation;
        if (options.conflict)
          return json({ detail: "记忆已更新，请重新读取后再决定" }, 409);
        if (options.failure) return json({ detail: "维护暂时失败" }, 500);
        expect(body.expected_revision).toBe(item.revision);
        const { expected_revision: revision, ...changes } = body;
        const updated = { ...item, ...changes, revision: Number(revision) + 1 };
        if (method === "PATCH") {
          updated.provenance = "owner_assertion";
          updated.corrections = [
            {
              revision: updated.revision,
              actor: "owner",
              created_at: "2026-10-09T00:00:00Z",
              prior: item,
              current: { ...updated, corrections: [] },
            },
          ];
        } else if (body.action === "delete") {
          items = items.filter((value) => value.id !== item.id);
          return json({
            id: item.id,
            revision: updated.revision,
            status: "deleted",
          });
        } else
          updated.status = body.action === "archive" ? "archived" : "active";
        items = items.map((value) => (value.id === item.id ? updated : value));
        return json(updated);
      }
      throw new Error("Unexpected endpoint: " + method + " " + url.pathname);
    }),
  );
  return { requests, options };
}

function open() {
  render(<MemoryPage api={new LingApi(token, vi.fn())} agent={agent} />);
  return userEvent.setup();
}

test("更正事件保留原文提示和历史，并采用新版本归档及恢复", async () => {
  const server = backend();
  const user = open();
  await user.click(await screen.findByRole("button", { name: "一起整理书桌" }));
  await user.click(screen.getByRole("button", { name: "更正" }));
  await user.clear(screen.getByLabelText("记忆摘要"));
  await user.type(screen.getByLabelText("记忆摘要"), "先把书分类，再收纳。");
  await user.click(screen.getByRole("button", { name: "保存更正" }));
  expect(await screen.findByText("已保存更正")).toBeInTheDocument();
  expect(screen.getByText("主人更正")).toBeInTheDocument();
  expect(screen.getByText(/原始记录.*保留/)).toBeInTheDocument();
  await user.click(screen.getByText("更正历史"));
  expect(screen.getByText("先收好所有书。")).toBeInTheDocument();
  expect(server.requests.find((r) => r.method === "PATCH")?.body).toEqual({
    expected_revision: 1,
    title: "一起整理书桌",
    summary: "先把书分类，再收纳。",
  });
  await user.click(screen.getByRole("button", { name: "归档" }));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("已归档"),
  );
  await user.click(screen.getByRole("button", { name: "恢复使用" }));
  await screen.findByText("已恢复使用");
  expect(
    server.requests.filter((r) => r.method === "POST").map((r) => r.body),
  ).toEqual([
    { expected_revision: 2, action: "archive" },
    { expected_revision: 3, action: "restore" },
  ]);
  expect(document.body).not.toHaveTextContent("episode-private");
});

test("知识格式切换，更正后明确确认删除且不删除原始历史", async () => {
  const server = backend();
  const user = open();
  await screen.findByRole("button", { name: "一起整理书桌" });
  await user.click(screen.getByRole("tab", { name: "知识" }));
  await user.click(
    await screen.findByRole("button", { name: "更喜欢安静阅读。" }),
  );
  await user.click(screen.getByRole("button", { name: "更正" }));
  await user.clear(screen.getByLabelText("知识主张"));
  await user.type(
    screen.getByLabelText("知识主张"),
    "外部报告提到偏好纸质书。",
  );
  await user.click(screen.getByRole("button", { name: "保存更正" }));
  await screen.findByText("已保存更正");
  await user.click(screen.getByRole("button", { name: "删除" }));
  const dialog = screen.getByRole("dialog", { name: "删除这条记忆？" });
  expect(dialog).toHaveTextContent(/原始.*保留/);
  expect(server.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "删除" }));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认删除",
    }),
  );
  await screen.findByText("已删除记忆条目，原始记录仍保留。");
  expect(
    server.requests
      .filter((r) => r.method === "PATCH" || r.method === "POST")
      .map((r) => ({
        path: r.url.pathname,
        body: r.body,
      })),
  ).toEqual([
    {
      path: "/api/memory/knowledge/knowledge-private",
      body: {
        expected_revision: 1,
        statement: "外部报告提到偏好纸质书。",
      },
    },
    {
      path: "/api/memory/knowledge/knowledge-private/state",
      body: {
        expected_revision: 2,
        action: "delete",
      },
    },
  ]);
  expect(
    server.requests.some((r) => r.url.pathname.startsWith("/api/sources/")),
  ).toBe(false);
});

test("版本冲突保留草稿，不自动重试覆盖，重新读取后才能再次保存", async () => {
  const server = backend({ conflict: true });
  const user = open();
  await user.click(await screen.findByRole("button", { name: "一起整理书桌" }));
  await user.click(screen.getByRole("button", { name: "更正" }));
  await user.clear(screen.getByLabelText("记忆摘要"));
  await user.type(screen.getByLabelText("记忆摘要"), "保留我的草稿");
  await user.click(screen.getByRole("button", { name: "保存更正" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/重新读取/);
  expect(screen.getByLabelText("记忆摘要")).toHaveValue("保留我的草稿");
  expect(screen.getByRole("button", { name: "保存更正" })).toBeDisabled();
  expect(server.requests.filter((r) => r.method === "PATCH")).toHaveLength(1);
  server.options.conflict = false;
  await user.click(screen.getByRole("button", { name: "重新读取记忆" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "更正" })).toBeEnabled(),
  );
  expect(screen.queryByText("已保存更正")).not.toBeInTheDocument();
});

test("失效条目可删除，但不能更正或恢复", async () => {
  backend({ invalid: true });
  const user = open();
  await user.click(await screen.findByRole("button", { name: "一起整理书桌" }));
  expect(screen.getByText(/来源已失效/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "更正" })).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "恢复使用" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "删除" })).toBeEnabled();
});

test("删除响应在切换到知识笔记后到达，不覆盖新页面或显示旧操作结果", async () => {
  let finish!: () => void;
  const waitMutation = new Promise<void>((resolve) => {
    finish = resolve;
  });
  backend({ waitMutation });
  const user = open();
  await user.click(await screen.findByRole("button", { name: "一起整理书桌" }));
  await user.click(screen.getByRole("button", { name: "删除" }));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认删除",
    }),
  );
  await user.click(screen.getByRole("tab", { name: "知识" }));
  await user.click(
    await screen.findByRole("button", { name: "更喜欢安静阅读。" }),
  );
  finish();
  await waitFor(() =>
    expect(screen.getByRole("region", { name: "记忆详情" })).toHaveTextContent(
      "更喜欢安静阅读。",
    ),
  );
  expect(
    screen.queryByText("已删除记忆条目，原始记录仍保留。"),
  ).not.toBeInTheDocument();
});

test("服务器拒绝更正时保留草稿，不提前显示新版本或成功", async () => {
  backend({ failure: true });
  const user = open();
  await user.click(await screen.findByRole("button", { name: "一起整理书桌" }));
  await user.click(screen.getByRole("button", { name: "更正" }));
  await user.type(screen.getByLabelText("记忆摘要"), "新增草稿");
  await user.click(screen.getByRole("button", { name: "保存更正" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("维护暂时失败");
  expect(screen.getByLabelText("记忆摘要")).toHaveValue(
    "先收好所有书。新增草稿",
  );
  expect(screen.getByRole("region", { name: "记忆详情" })).toHaveTextContent(
    "版本 1",
  );
  expect(screen.queryByText("已保存更正")).not.toBeInTheDocument();
});
