import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { TasksPage } from "./TasksPage";
import { agent, backend, stamp, task } from "./test/fixtures";

afterEach(() => vi.useRealTimers());

test("任务提交、真实事件与结果、审阅确认使用服务返回的 review_id", async () => {
  let saved = { ...task };
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.startsWith("/api/tasks?") && method === "GET") return [saved];
    if (path === "/api/tasks" && method === "POST") {
      saved = { ...saved, objective: String(body.objective) };
      return saved;
    }
    if (path === "/api/tasks/t1") return saved;
    if (path.startsWith("/api/tasks/t1/events?"))
      return [
        {
          id: 1,
          task_id: "t1",
          type: "tool_result",
          data: { name: "read_file", result: { text: "真实文件内容" } },
          created_at: stamp,
        },
      ];
    if (path === "/api/tasks/t1/changes")
      return {
        review_id: "review-exact",
        files: [
          {
            path: "说明.md",
            before: "old",
            after: "new",
            operation: "modify",
            diff: "-旧说明\n+新说明",
            artifact: "说明.md",
          },
        ],
      };
    if (path === "/api/tasks/t1/apply")
      return {
        status: "applied",
        review_id: body.review_id,
        effect_ids: ["effect-1"],
      };
  });
  const user = userEvent.setup();
  render(<TasksPage api={api} agent={agent} />);
  await user.type(await screen.findByLabelText("任务目标"), "整理 README");
  await user.click(screen.getByRole("button", { name: "创建任务" }));
  await waitFor(() =>
    expect(
      requests.some((r) => r.method === "POST" && r.path === "/api/tasks"),
    ).toBe(true),
  );
  expect(
    requests.find((r) => r.path === "/api/tasks" && r.method === "POST")?.body,
  ).toEqual({
    agent_id: "a/1",
    objective: "整理 README",
    environment: "python",
    request_id: expect.any(String),
  });
  const records = await screen.findByRole("region", { name: "任务执行记录" });
  await user.click(within(records).getByText(/实际执行记录/));
  await user.click(within(records).getByText("查看完整记录"));
  expect((await screen.findAllByText(/真实文件内容/))[0]).toBeVisible();
  await user.click(screen.getByRole("button", { name: "审阅文件改动" }));
  await screen.findByText(/新说明/);
  await user.click(screen.getByRole("button", { name: "应用这些改动" }));
  expect(requests.filter((r) => r.path.endsWith("/apply"))).toHaveLength(0);
  const dialog = screen.getByRole("dialog", { name: "将改动写回项目" });
  expect(dialog).toHaveTextContent("H:/project");
  await user.click(
    within(dialog).getByRole("button", { name: "确认写回项目" }),
  );
  await screen.findByText("改动已写回项目。");
  expect(requests.find((r) => r.path.endsWith("/apply"))?.body).toEqual({
    review_id: "review-exact",
  });
  expect(
    requests.every(
      (r) =>
        new Headers(r.options.headers).get("Authorization") ===
        "Bearer fixture-owner-session",
    ),
  ).toBe(true);
});

test("任务暂停、恢复、取消；卸载只停止轮询和读取", async () => {
  let saved = { ...task, status: "running" };
  const { api, requests } = backend(({ path }) => {
    if (path.startsWith("/api/tasks?") || path === "/api/tasks/t1")
      return path.includes("?") ? [saved] : saved;
    if (path.includes("/events?")) return [];
    for (const [action, status] of [
      ["pause", "paused"],
      ["resume", "running"],
      ["cancel", "cancelled"],
    ]) {
      if (path.endsWith("/" + action)) {
        saved = { ...saved, status };
        return saved;
      }
    }
  });
  const user = userEvent.setup();
  const view = render(<TasksPage api={api} agent={agent} />);
  await user.click(await screen.findByRole("button", { name: "查看任务" }));
  await user.click(await screen.findByRole("button", { name: "暂停任务" }));
  await user.click(await screen.findByRole("button", { name: "恢复任务" }));
  expect(requests.find((r) => r.path.endsWith("/resume"))?.body).toEqual({
    additional_steps: 0,
  });
  vi.useFakeTimers();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(
    requests.filter((r) => r.path.startsWith("/api/tasks?")).length,
  ).toBeGreaterThan(1);
  const count = requests.length;
  view.unmount();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(requests).toHaveLength(count);
  expect(requests.some((r) => r.path.endsWith("/cancel"))).toBe(false);
  expect(
    requests
      .filter((r) => r.method === "GET")
      .every((r) => r.options.signal?.aborted),
  ).toBe(true);
});

test("选择 Node.js 环境提交；等待浏览器确认的任务必须显式恢复", async () => {
  let saved = { ...task, status: "waiting_owner" };
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.startsWith("/api/tasks?")) return [saved];
    if (path === "/api/tasks" && method === "POST") return saved;
    if (path === "/api/tasks/t1") return saved;
    if (path.includes("/events?")) return [];
    if (path.endsWith("/resume")) {
      saved = { ...saved, status: "running" };
      return saved;
    }
    const origins = { agent_id: agent.id, revision: 0, grants: [] };
    if (path.endsWith("/browser/origins")) return origins;
    if (path.endsWith("/browser/state"))
      return { agent_id: agent.id, origins, page: null };
    if (path.endsWith("/browser/approvals")) return [];
    throw new Error("Unexpected route " + path + String(body));
  });
  const user = userEvent.setup();
  render(<TasksPage api={api} agent={agent} />);
  await user.type(
    await screen.findByLabelText("任务目标"),
    "验证 TypeScript 项目",
  );
  await user.selectOptions(screen.getByLabelText("执行环境"), "node");
  await user.click(screen.getByRole("button", { name: "创建任务" }));
  await screen.findByRole("button", { name: "前往浏览器确认" });
  expect(
    requests.find((r) => r.path === "/api/tasks" && r.method === "POST")?.body
      .environment,
  ).toBe("node");
  expect(screen.getAllByText("需确认").length).toBeGreaterThan(0);
  await user.click(screen.getByRole("button", { name: "前往浏览器确认" }));
  await screen.findByRole("heading", { name: "浏览器" });
  expect(requests.some((r) => r.path.endsWith("/resume"))).toBe(false);
  await user.click(screen.getByRole("button", { name: "恢复任务" }));
  await waitFor(() =>
    expect(requests.find((r) => r.path.endsWith("/resume"))?.body).toEqual({
      additional_steps: 0,
    }),
  );
});
