import { expect, test, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../../api/client";
import type { SandboxLevel } from "../../api/types";
import { SandboxSettings } from "./SandboxSettings";

const PROTECTIONS = [
  "文件工具只能在任务工作副本内读写（路径授权、拒绝父目录穿越与符号链接）",
  "命令以参数列表直接执行，不经过系统命令行解释器，避免命令拼接注入",
];
const LIMITATIONS = [
  "命令进程本身没有文件系统隔离：仍可读写工作副本以外的文件",
  "不能阻止模型被恶意指令诱导后主动外传数据",
];

function backend(
  options: {
    engine?: boolean;
    initialLevel?: SandboxLevel;
    failSwitch?: boolean;
  } = {},
) {
  const engine = options.engine ?? false;
  let level: SandboxLevel = options.initialLevel ?? "local";
  const requests: { path: string; method: string; body: any }[] = [];
  const state = () => ({
    level,
    available_levels: engine ? ["local", "container"] : ["local"],
    engine: { available: engine, name: engine ? "docker" : null },
    protections: PROTECTIONS,
    limitations: LIMITATIONS,
    image: "python:3.12-slim",
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : null;
      requests.push({ path, method, body });
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), { status });
      if (path === "/api/sandbox") {
        if (method === "PUT") {
          if (options.failSwitch)
            return json(
              { detail: "本机没有可用的容器引擎，无法切换到容器隔离。" },
              403,
            );
          level = body.level;
        }
        return json(state());
      }
      throw new Error("Unexpected endpoint: " + method + " " + path);
    }),
  );
  return { requests, level: () => level };
}

function show() {
  const user = userEvent.setup();
  render(<SandboxSettings api={new LingApi("sandbox-test-owner", () => {})} />);
  return { user, region: () => screen.getByRole("region", { name: "执行沙箱" }) };
}

test("读取当前档位，并如实展示能防与不能防的清单", async () => {
  const server = backend();
  const { region } = show();
  const panel = region();
  expect(await within(panel).findByText("当前档位")).toBeInTheDocument();
  for (const item of PROTECTIONS) {
    expect(within(panel).getByText(item)).toBeInTheDocument();
  }
  for (const item of LIMITATIONS) {
    expect(within(panel).getByText(item)).toBeInTheDocument();
  }
  expect(within(panel).getByText("它能防住")).toBeInTheDocument();
  expect(within(panel).getByText("它防不住")).toBeInTheDocument();
  expect(server.requests.filter((r) => r.method === "GET")).toHaveLength(1);
});

test("引擎不可用时容器档不可选，并说明需要先安装容器引擎", async () => {
  const server = backend({ engine: false });
  const { region } = show();
  const panel = region();
  expect(
    await within(panel).findByRole("button", { name: "切换到容器隔离" }),
  ).toBeDisabled();
  expect(
    within(panel).getByRole("button", { name: "切换到本地受限执行" }),
  ).toBeDisabled();
  expect(
    within(panel).getByText(/需要先安装容器引擎/),
  ).toBeInTheDocument();
  expect(server.requests.some((r) => r.method === "PUT")).toBe(false);
});

test("引擎可用时切换到容器档需要先确认，确认后保存并更新展示", async () => {
  const server = backend({ engine: true });
  const { user, region } = show();
  const panel = region();
  const container = await within(panel).findByRole("button", {
    name: "切换到容器隔离",
  });
  expect(container).toBeEnabled();
  await user.click(container);
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent("更严格的隔离");
  expect(server.requests.some((r) => r.method === "PUT")).toBe(false);
  await user.click(
    within(dialog).getByRole("button", { name: "确认切换到容器隔离" }),
  );
  await within(panel).findByText(/执行沙箱已切换到「容器隔离」/);
  expect(server.requests.find((r) => r.method === "PUT")?.body).toEqual({
    level: "container",
  });
  expect(server.level()).toBe("container");
  expect(
    within(panel).getByRole("button", { name: "切换到容器隔离" }),
  ).toBeDisabled();
  expect(
    within(panel).getByRole("button", { name: "切换到本地受限执行" }),
  ).toBeEnabled();
});

test("从容器档切回本地档无需确认，直接保存", async () => {
  const server = backend({ engine: true, initialLevel: "container" });
  const { user, region } = show();
  const panel = region();
  const local = await within(panel).findByRole("button", {
    name: "切换到本地受限执行",
  });
  expect(local).toBeEnabled();
  await user.click(local);
  await within(panel).findByText(/执行沙箱已切换到「本地受限执行」/);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(server.requests.find((r) => r.method === "PUT")?.body).toEqual({
    level: "local",
  });
  expect(server.level()).toBe("local");
});

test("切换失败时显示错误且不改动当前档位", async () => {
  const server = backend({ engine: true, failSwitch: true });
  const { user, region } = show();
  const panel = region();
  await user.click(
    await within(panel).findByRole("button", { name: "切换到容器隔离" }),
  );
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认切换到容器隔离",
    }),
  );
  expect(await within(panel).findByRole("alert")).toHaveTextContent(
    /无法切换到容器隔离/,
  );
  expect(within(panel).queryByText(/执行沙箱已切换到/)).not.toBeInTheDocument();
  expect(server.level()).toBe("local");
  expect(
    within(panel).getByRole("button", { name: "切换到容器隔离" }),
  ).toBeEnabled();
});
