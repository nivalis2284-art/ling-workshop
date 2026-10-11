import { expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../app/App";

const token = "test-only-owner-session";
const agent = { id: "ling-1", name: "小灵" };

function backend(options: { noAgents?: boolean } = {}) {
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
      if (url.pathname === "/api/maintenance/backup")
        return json({ saved: true });
      if (url.pathname === "/api/models") return json([]);
      if (url.pathname === "/api/models/selection")
        return json({ primary: null, fallback: null });
      if (url.pathname === "/api/agents/ling-1/instructions")
        return json({
          agent_id: agent.id,
          rules: ["默认指令"],
          defaults: ["默认指令"],
          customized: false,
        });
      throw new Error(`Unexpected network request: ${method} ${url.pathname}`);
    }),
  );
  return requests;
}

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
