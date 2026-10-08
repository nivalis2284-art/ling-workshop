import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { BackgroundPage } from "./BackgroundPage";
import { agent, backend, stamp } from "../tasks/test/fixtures";

test("后台开关、运行一次与失败重试使用现有全局路由并报告实际结果", async () => {
  let enabled = true;
  let jobStatus = "failed";
  const { api, requests } = backend(({ path, method, body }) => {
    if (path === "/api/background") {
      if (method === "PUT") enabled = Boolean(body.enabled);
      return {
        enabled,
        working: false,
        queued: 0,
        last_result: null,
        interval_seconds: 20,
      };
    }
    if (path.startsWith("/api/background/jobs?"))
      return [
        {
          id: "j1",
          agent_id: agent.id,
          kind: "consolidate",
          status: jobStatus,
          created_at: stamp,
          attempts: 1,
          outcome: "failed",
          error_code: "timeout",
        },
      ];
    if (path.endsWith("/j1/retry")) {
      jobStatus = "queued";
      return { job_id: "j1", status: "queued" };
    }
    if (path.endsWith("/run-once")) return { status: "foreground_busy" };
  });
  const user = userEvent.setup();
  render(<BackgroundPage api={api} agent={agent} />);
  await user.click(await screen.findByRole("button", { name: "暂停后台" }));
  await user.click(await screen.findByRole("button", { name: "启用后台" }));
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual(
    [{ enabled: false }, { enabled: true }],
  );
  await user.click(screen.getByRole("button", { name: "运行一次" }));
  await screen.findByText(/正在处理前台工作/);
  await user.click(screen.getByRole("button", { name: "重试失败工作" }));
  expect(requests.some((r) => r.path.endsWith("/retry"))).toBe(false);
  await user.click(screen.getByRole("button", { name: "确认重试" }));
  await waitFor(() =>
    expect(
      requests.some((r) => r.path === "/api/background/jobs/j1/retry"),
    ).toBe(true),
  );
  await screen.findByText("等待处理");
});
