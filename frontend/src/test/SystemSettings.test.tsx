import { expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../api/client";
import { SystemSettings } from "../features/settings/SystemSettings";

function backend(
  options: {
    failSave?: boolean;
    failCommit?: boolean;
    queued?: boolean;
    desktop?: boolean;
  } = {},
) {
  const requests: { path: string; method: string; body: any }[] = [];
  let backups = {
    enabled: true,
    path: "C:/Ling/backups",
    retention: 14,
    last_success_day: null,
    last_success_at: null,
    last_backup: null,
    last_error: null,
  };
  let startup = { available: true, enabled: false };
  let restore = {
    status: options.queued ? "queued" : "idle",
    plan_id: "a".repeat(32),
    quit_available: !!options.desktop,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : null;
      requests.push({ path, method, body });
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), { status });
      if (path === "/api/system/backups") {
        if (method === "PUT") {
          if (options.failSave) return json({ detail: "磁盘无法访问" }, 409);
          backups = { ...backups, ...body };
        }
        return json(backups);
      }
      if (path === "/api/system/startup") {
        if (method === "PUT") startup = { ...startup, ...body };
        return json(startup);
      }
      if (path === "/api/system/restore") return json(restore);
      if (path.endsWith("/restore/prepare"))
        return json({
          status: "prepared",
          plan_id: "a".repeat(32),
          summary: { agents: 2, sources: 4, attachments: 1 },
        });
      if (path.endsWith("/restore/commit")) {
        if (options.failCommit)
          return json({ detail: "来源已变化，请重新预览" }, 422);
        restore = { ...restore, status: "queued" };
        return json({ ...restore, restart_required: true });
      }
      if (path.endsWith("/restore/quit"))
        return json({
          quit_requested: !!options.desktop,
          restart_required: true,
        });
      throw new Error("Unexpected endpoint: " + path);
    }),
  );
  return requests;
}

function show() {
  render(<SystemSettings api={new LingApi("system-test-owner", () => {})} />);
  return userEvent.setup();
}

test("backup settings and login startup persist only on explicit saves, with honest backup evidence", async () => {
  const requests = backend();
  const user = show();
  const path = await screen.findByLabelText("自动备份目录");
  expect(
    screen.getByText("尚无成功的自动备份记录。每天在空闲时尝试一次。"),
  ).toBeInTheDocument();
  await user.clear(path);
  await user.type(path, "D:/My Backups");
  await user.clear(screen.getByLabelText("保留成功备份份数"));
  await user.type(screen.getByLabelText("保留成功备份份数"), "7");
  expect(requests.every((r) => r.method === "GET")).toBe(true);
  await user.click(screen.getByRole("button", { name: "保存自动备份设置" }));
  await screen.findByText("自动备份设置已保存。下次空闲时按每日计划执行。");
  expect(requests.find((r) => r.method === "PUT")?.body).toEqual({
    enabled: true,
    path: "D:/My Backups",
    retention: 7,
  });
  expect(screen.queryByText("备份已完成")).not.toBeInTheDocument();
  await user.click(screen.getByLabelText("登录 Windows 时启动 LingNexus"));
  await user.click(screen.getByRole("button", { name: "保存登录启动设置" }));
  await waitFor(() =>
    expect(
      requests.find((r) => r.path.endsWith("/startup") && r.method === "PUT")
        ?.body,
    ).toEqual({ enabled: true }),
  );
});

test("restore requires a verified preview and confirmation; commit only queues, quit is a separate action", async () => {
  const requests = backend({ desktop: true });
  const user = show();
  await user.type(
    await screen.findByLabelText("恢复来源路径"),
    "C:/Saved/backup.db",
  );
  await user.click(screen.getByRole("button", { name: "验证并预览恢复" }));
  await screen.findByText("2 位灵 · 4 条来源 · 1 个附件");
  expect(requests.some((r) => r.path.endsWith("/commit"))).toBe(false);
  await user.click(screen.getByRole("button", { name: "排队恢复" }));
  const dialog = screen.getByRole("dialog");
  await user.click(
    within(dialog).getByRole("button", { name: "确认在下次启动恢复" }),
  );
  await screen.findByText(
    "恢复已排队。请完全退出 LingNexus 后重新打开，恢复将在下次桌面启动时应用。",
  );
  expect(requests.find((r) => r.path.endsWith("/commit"))?.body).toEqual({
    plan_id: "a".repeat(32),
    confirmed: true,
  });
  expect(requests.some((r) => r.path.endsWith("/quit"))).toBe(false);
  expect(screen.queryByText("资料已恢复")).not.toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "退出 LingNexus，稍后重新打开" }),
  );
  await waitFor(() =>
    expect(requests.some((r) => r.path.endsWith("/quit"))).toBe(true),
  );
});

test("editing the source invalidates the preview and failed commit does not show queued success", async () => {
  backend({ failCommit: true });
  const user = show();
  const path = await screen.findByLabelText("恢复来源路径");
  await user.type(path, "C:/backup.db");
  await user.click(screen.getByRole("button", { name: "验证并预览恢复" }));
  await screen.findByRole("button", { name: "排队恢复" });
  await user.type(path, "2");
  expect(
    screen.queryByRole("button", { name: "排队恢复" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "验证并预览恢复" }));
  await user.click(await screen.findByRole("button", { name: "排队恢复" }));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认在下次启动恢复",
    }),
  );
  await screen.findByText("来源已变化，请重新预览");
  expect(screen.queryByText(/恢复已排队/)).not.toBeInTheDocument();
});

test("queued restore survives reload and explains manual restart without a desktop callback", async () => {
  const requests = backend({ queued: true });
  show();
  await screen.findByText(
    "恢复已排队。请完全退出 LingNexus 后重新打开，恢复将在下次桌面启动时应用。",
  );
  expect(
    screen.queryByRole("button", { name: "退出 LingNexus，稍后重新打开" }),
  ).not.toBeInTheDocument();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test("failed backup settings save shows the error and no success claim", async () => {
  backend({ failSave: true });
  const user = show();
  await screen.findByLabelText("自动备份目录");
  await user.click(screen.getByRole("button", { name: "保存自动备份设置" }));
  await screen.findByText("磁盘无法访问");
  expect(screen.queryByText(/自动备份设置已保存/)).not.toBeInTheDocument();
});

test("initial system load failure stays in its panel and can be retried", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new Error("offline");
    }),
  );
  const user = show();
  const panel = screen.getByRole("region", { name: "备份、恢复与启动" });
  await within(panel).findByText(/暂时连接不到本地服务/);
  expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
  backend();
  await user.click(
    within(panel).getByRole("button", { name: "重新读取系统设置" }),
  );
  await screen.findByLabelText("自动备份目录");
});
