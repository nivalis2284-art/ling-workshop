import { expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../app/App";

const token = "test-only-owner-session";
const agent = { id: "ling-1", name: "小灵" };
const ref = { id: "source-1", revision: 3 };
const proposal = {
  id: "proposal-1",
  key: "identity.description",
  text: "耐心倾听，再给出建议。",
  status: "awaiting_owner",
  sources: [ref],
  base_revision: 7,
  kind: "identity",
};
const episode = {
  id: "episode-1",
  title: "一起整理书桌",
  summary: "决定先把书分类。",
  scope: "real",
  sources: [ref],
  revision: 1,
  status: "active",
  agent_id: agent.id,
  source_valid: true,
  provenance: "derived",
  corrections: [],
};

function backend(
  options: {
    noAgents?: boolean;
    conflict?: boolean;
    sourceMissing?: boolean;
  } = {},
) {
  let revision = 7;
  let status = "awaiting_owner";
  let entries: Record<string, string> = {};
  let agents = options.noAgents ? [] : [agent];
  const requests: {
    url: URL;
    method: string;
    body: Record<string, unknown> | null;
  }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("Authorization")).toBe(
        `Bearer ${token}`,
      );
      const url = new URL(input, "http://localhost");
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      requests.push({ url, method, body });
      const json = (value: unknown, code = 200) =>
        new Response(JSON.stringify(value), {
          status: code,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/agents" && method === "GET")
        return json(agents);
      if (url.pathname === "/api/system/backups")
        return json({enabled:true,path:"C:/LingData/backups",retention:14,last_success_day:null,last_success_at:null,last_backup:null,last_error:null});
      if (url.pathname === "/api/system/startup")
        return json({available:true,enabled:false});
      if (url.pathname === "/api/system/restore")
        return json({status:"idle",quit_available:false});
      if (url.pathname === "/api/agents" && method === "POST") {
        agents = [{ ...agent, name: body.name }];
        return json({ id: agent.id }, 201);
      }
      if (url.pathname.endsWith("/state"))
        return json({ agent_id: agent.id, revision, entries });
      if (url.pathname.endsWith("/entries"))
        return json({
          agent_id: agent.id,
          entries: Object.entries(entries).map(([key, text]) => ({
            key,
            text,
            kind: "identity",
            origin: {
              proposal_id: proposal.id,
              kind: "identity",
              sources: [ref],
            },
          })),
        });
      if (url.pathname.endsWith("/instructions"))
        return json({
          agent_id: agent.id,
          rules: ["始终使用中文回复"],
          defaults: ["始终使用中文回复"],
          customized: false,
        });
      if (url.pathname.endsWith("/proposals"))
        return json([{ ...proposal, status }]);
      if (url.pathname.endsWith("/revisions"))
        return json([
          {
            revision,
            entries,
            reason: "initialize",
            created_at: "2026-10-08T10:00:00Z",
          },
          {
            revision: 0,
            entries: {},
            reason: "initialize",
            created_at: "2026-10-01T10:00:00Z",
          },
        ]);
      if (url.pathname === "/api/proposals/proposal-1/decision") {
        if (options.conflict && body.decision === "approve") {
          revision = 8;
          entries = { "identity.description": "另一窗口已更新的理解" };
          return json({ detail: "当前版本与候选不一致，需要重新评估" }, 409);
        }
        expect(body.expected_revision).toBe(revision);
        if (body.decision === "approve") {
          revision += 1;
          entries = { [proposal.key]: proposal.text };
          status = "applied_owner";
        } else status = "rejected";
        return json({ ...proposal, status });
      }
      if (url.pathname.endsWith("/rollback")) {
        expect(body.expected_revision).toBe(revision);
        expect(body.target_revision).toBe(0);
        revision += 1;
        entries = {};
        return json({ agent_id: agent.id, revision, entries });
      }
      if (url.pathname.endsWith("/episodes"))
        return json(
          url.searchParams.get("query") === "不存在" ? [] : [episode],
        );
      if (url.pathname === "/api/memory/ling-1/items") {
        expect(url.searchParams.get("kind")).toBe("episodes");
        expect(url.searchParams.get("status")).toBe("all");
        return json(
          url.searchParams.get("query") === "不存在" ? [] : [episode],
        );
      }
      if (url.pathname === "/api/memory/episodes/episode-1")
        return json(episode);
      if (url.pathname === "/api/sources/source-1") {
        expect(url.searchParams.get("revision")).toBe("3");
        return options.sourceMissing
          ? json({ detail: "经历已被纠正，请读取当前版本" }, 422)
          : json({
              ...ref,
              text: "先整理书，再慢慢收纳其他东西。",
              kind: "user",
              scope: "real",
            });
      }
      if (url.pathname === "/api/maintenance/backup")
        return json({ saved: true });
      if (url.pathname === "/api/models") return json([]);
      if (url.pathname === "/api/models/selection")
        return json({ primary: null, fallback: null });
      throw new Error(`Unexpected network request: ${method} ${url.pathname}`);
    }),
  );
  return requests;
}

async function openReview() {
  const user = userEvent.setup();
  render(<App initialToken={token} />);
  await user.click(await screen.findByRole("button", { name: "身份审阅" }));
  await screen.findByText(proposal.text);
  return user;
}

test("批准使用正在审阅的当前版本，完成后显示后端的新版本与身份", async () => {
  const requests = backend();
  const user = await openReview();
  expect(screen.getByText("当前版本 · 7")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "批准这项变化" }));
  await screen.findByText("当前版本 · 8");
  expect(
    within(screen.getByRole("region", { name: "当前身份" })).getByText(
      proposal.text,
    ),
  ).toBeInTheDocument();
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({
    decision: "approve",
    expected_revision: 7,
  });
  expect(
    screen.queryByRole("button", { name: "批准这项变化" }),
  ).not.toBeInTheDocument();
});

test("版本冲突刷新当前身份，不自动重试批准过期候选", async () => {
  const requests = backend({ conflict: true });
  const user = await openReview();
  await user.click(screen.getByRole("button", { name: "批准这项变化" }));
  await screen.findByText("当前版本 · 8");
  expect(
    within(screen.getByRole("region", { name: "当前身份" })).getByText(
      "另一窗口已更新的理解",
    ),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "批准这项变化" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("版本");
  expect(requests.filter((request) => request.method === "POST")).toHaveLength(
    1,
  );
  await user.click(screen.getByRole("button", { name: "拒绝这项变化" }));
  await waitFor(() =>
    expect(
      requests.filter((request) => request.method === "POST")[1]?.body,
    ).toEqual({ decision: "reject", expected_revision: 8 }),
  );
});

test("记忆详情可追溯到指定修订的来源原文，关闭后保留浏览上下文", async () => {
  const requests = backend();
  const user = userEvent.setup();
  render(<App initialToken={token} />);
  await user.click(await screen.findByRole("button", { name: "记忆" }));
  await user.click(await screen.findByRole("button", { name: "一起整理书桌" }));
  await user.click(
    await screen.findByRole("button", { name: "查看来源 1，版本 3" }),
  );
  expect(
    await screen.findByText("先整理书，再慢慢收纳其他东西。"),
  ).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "来源原文" })).toHaveTextContent(
    "版本 3",
  );
  expect(
    requests.some(
      (request) => request.url.pathname === "/api/memory/episodes/episode-1",
    ),
  ).toBe(true);
  await user.click(screen.getByRole("button", { name: "关闭来源" }));
  expect(
    screen.getByRole("heading", { name: episode.title }),
  ).toBeInTheDocument();
});

test("失效来源显示错误，不用其他修订替换引用", async () => {
  const requests = backend({ sourceMissing: true });
  const user = await openReview();
  await user.click(screen.getByRole("button", { name: "查看来源 1，版本 3" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("经历已被纠正");
  expect(
    requests
      .filter((request) => request.url.pathname.startsWith("/api/sources/"))
      .every((request) => request.url.searchParams.get("revision") === "3"),
  ).toBe(true);
});

test("没有所有者会话时提供启动指引且不访问受保护接口", () => {
  const requests = backend();
  render(<App initialToken={null} />);
  expect(
    screen.getByRole("heading", { name: "连接你的本地空间" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("list")).toHaveTextContent("启动器");
  expect(requests).toHaveLength(0);
});

test("首次进入可创建一个灵并加载真实数据", async () => {
  const requests = backend({ noAgents: true });
  const user = userEvent.setup();
  render(<App initialToken={token} />);
  const input = await screen.findByRole("textbox", { name: "灵的名字" });
  await user.clear(input);
  await user.type(input, "初见");
  await user.click(screen.getByRole("button", { name: "创建我的灵" }));
  expect(
    await screen.findByRole("heading", { name: "初见的空间" }),
  ).toBeInTheDocument();
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({
    name: "初见",
  });
});

test("备份经确认后将 Windows 目标路径发送给后端", async () => {
  const requests = backend();
  const destination = ["C:", "LingData", "backup.db"].join(
    String.fromCharCode(92),
  );
  const user = userEvent.setup();
  render(<App initialToken={token} />);
  await user.click(await screen.findByRole("button", { name: "设置" }));
  await user.type(
    screen.getByRole("textbox", { name: "备份文件路径" }),
    destination,
  );
  await user.click(screen.getByRole("button", { name: "创建备份" }));
  expect(requests.filter((request) => request.method === "POST")).toHaveLength(
    0,
  );
  await user.click(screen.getByRole("button", { name: "确认创建备份" }));
  expect(await screen.findByText("备份已完成："+destination)).toBeVisible();
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({
    path: destination,
  });
});

test("回退展示目标版本，确认后发送目标与当前版本并读取新状态", async () => {
  const requests = backend();
  const user = await openReview();
  await user.click(screen.getByRole("button", { name: "恢复到版本 0" }));
  expect(
    screen.getByRole("dialog", { name: "恢复到版本 0" }),
  ).toHaveTextContent("新版本");
  await user.click(screen.getByRole("button", { name: "确认恢复" }));
  await screen.findByText("当前版本 · 8");
  expect(
    requests.find((request) => request.url.pathname.endsWith("/rollback"))
      ?.body,
  ).toEqual({ target_revision: 0, expected_revision: 7 });
});

test("搜索空结果来自服务，清空搜索后可恢复记忆列表", async () => {
  const requests = backend();
  const user = userEvent.setup();
  render(<App initialToken={token} />);
  await user.click(await screen.findByRole("button", { name: "记忆" }));
  await screen.findByRole("button", { name: episode.title });
  await user.type(
    screen.getByRole("searchbox", { name: "搜索记忆" }),
    "不存在",
  );
  await user.click(screen.getByRole("button", { name: "搜索" }));
  await screen.findByText("没有找到相关记忆");
  const search = requests
    .filter((request) => request.url.pathname === "/api/memory/ling-1/items")
    .at(-1);
  expect(search?.url.searchParams.get("query")).toBe("不存在");
  expect(search?.url.searchParams.get("offset")).toBe("0");
  expect(search?.url.searchParams.get("limit")).toBe("20");
  await user.clear(screen.getByRole("searchbox", { name: "搜索记忆" }));
  await user.click(screen.getByRole("button", { name: "搜索" }));
  await screen.findByRole("button", { name: episode.title });
});

test("网络失败提供可重试入口，不把失败显示成空资料", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new TypeError("network unavailable")),
  );
  render(<App initialToken={token} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "确认 LingNexus 正在运行",
  );
  backend({ noAgents: true });
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByLabelText("灵的名字");
});
