import { expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../api/client";
import { RecoveryReview } from "../shared/RecoveryReview";

const path = "/api/execution/recovery/task/task-1";

function view(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "task-1",
    kind: "task",
    status: "interrupted",
    sessions: [{ session_id: "session-1", status: "unknown" }],
    uncertain_effects: [{ step: 3, status: "executing" }],
    reviews: [],
    can_abandon: true,
    guidance: "核对记录不证明命令已停止，不解除未知副作用的阻断。",
    version: "version-1",
    ...overrides,
  };
}

function setup(initial = view()) {
  let current = initial;
  const requests: { path: string; method: string; body: any }[] = [];
  const json = (value: unknown, status = 200) =>
    new Response(JSON.stringify(value), { status });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : null;
      requests.push({ path: request, method, body });
      if (request !== path) throw new Error("Unexpected endpoint: " + request);
      if (method === "GET") return json(current);
      if (body.expected_version !== current.version)
        return json({ detail: "核对记录已变化" }, 409);
      current = view({
        reviews: [
          ...current.reviews,
          {
            request_id: body.request_id,
            conclusion: body.conclusion,
            note: body.note,
            actor: "owner",
            created_at: "2026-01-01T00:00:00Z",
            cleanup_confirmed: false,
          },
        ],
        version: "version-2",
        can_abandon: false,
      });
      return json(current);
    }),
  );
  const api = new LingApi("recovery-test-owner", () => {});
  return { api, requests, current: () => current };
}

test("懒加载：未点击前不请求恢复记录，点击后才读取", async () => {
  const user = userEvent.setup();
  const { api, requests } = setup();
  render(<RecoveryReview api={api} runId="task-1" kind="task" />);
  expect(requests).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "读取恢复记录与证据" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toMatchObject({ path, method: "GET" });
  expect(await screen.findByText(/session-1/)).toBeTruthy();
  expect(screen.getByText(/第 3 步/)).toBeTruthy();
  expect(screen.getByText(/还没有任何人工核对记录/)).toBeTruthy();
});

test("人工核对只记录判断：不解除未知副作用阻断，且不清理进程", async () => {
  const user = userEvent.setup();
  const { api, requests } = setup();
  render(<RecoveryReview api={api} runId="task-1" kind="task" />);
  await user.click(screen.getByRole("button", { name: "读取恢复记录与证据" }));
  await user.type(await screen.findByLabelText(/备注/), "已核对进程输出");
  await user.click(screen.getByRole("button", { name: "记录为已人工核对" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toMatchObject({
    path,
    method: "POST",
    body: {
      expected_version: "version-1",
      conclusion: "reviewed",
      note: "已核对进程输出",
    },
  });
  expect(typeof requests[1].body.request_id).toBe("string");
  expect(requests[1].body.request_id.length).toBeGreaterThan(0);
  expect(
    await screen.findByText(/不会解除未知副作用的阻断，也不证明进程已停止/),
  ).toBeTruthy();
  // 记录不解锁：服务仍未确认清理，放弃仍被禁止，未知副作用仍在列。
  expect(screen.getByText(/程序未确认清理/)).toBeTruthy();
  expect(screen.getByText(/第 3 步/)).toBeTruthy();
  expect(
    (screen.getByRole("button", {
      name: "放弃恢复旧运行",
    }) as HTMLButtonElement).disabled,
  ).toBe(true);
});

test("放弃需要确认对话框，且仅 can_abandon 时可用", async () => {
  const user = userEvent.setup();
  const { api, requests } = setup();
  render(<RecoveryReview api={api} runId="task-1" kind="task" />);
  await user.click(screen.getByRole("button", { name: "读取恢复记录与证据" }));
  const abandon = await screen.findByRole("button", {
    name: "放弃恢复旧运行",
  });
  expect((abandon as HTMLButtonElement).disabled).toBe(false);
  await user.click(abandon);
  // 必须经过确认对话框：对话框打开本身不发出请求。
  expect(await screen.findByText(/不会清理任何仍在运行的进程/)).toBeTruthy();
  expect(requests).toHaveLength(1);
  // 备注是服务端硬要求（1–2000 字）：未填时确认不会发出任何请求。
  await user.click(screen.getByRole("button", { name: "确认放弃" }));
  expect(requests).toHaveLength(1);
  expect(
    (await screen.findAllByText(/请填写 1–2000 字的核对备注/)).length,
  ).toBeGreaterThan(0);
  // 补齐备注后按正确交互再次确认，才发出 abandon 请求。
  await user.type(await screen.findByLabelText(/备注/), "已核对，放弃恢复");
  await user.click(
    await screen.findByRole("button", { name: "放弃恢复旧运行" }),
  );
  await user.click(await screen.findByRole("button", { name: "确认放弃" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1].body.conclusion).toBe("abandon");
  expect(requests[1].body.note).toBe("已核对，放弃恢复");
  expect(await screen.findByText(/不会清理仍在运行的进程/)).toBeTruthy();
});
