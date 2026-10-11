import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { BackgroundPage } from "./BackgroundPage";
import { agent, backend } from "../tasks/test/fixtures";

test("后台只保留自动备份：开关与立即检查使用现有路由并报告实际结果", async () => {
  let enabled = true;
  let runResult: Record<string, unknown> = { status: "foreground_busy" };
  const { api, requests } = backend(({ path, method, body }) => {
    if (path === "/api/background") {
      if (method === "PUT") enabled = Boolean(body.enabled);
      return {
        enabled,
        working: false,
        last_result: null,
        interval_seconds: 20,
      };
    }
    if (path.endsWith("/run-once")) return runResult;
  });
  const user = userEvent.setup();
  render(<BackgroundPage api={api} agent={agent} />);
  await user.click(await screen.findByRole("button", { name: "暂停后台" }));
  await user.click(await screen.findByRole("button", { name: "启用后台" }));
  expect(requests.filter((r) => r.method === "PUT").map((r) => r.body)).toEqual(
    [{ enabled: false }, { enabled: true }],
  );
  await user.click(screen.getByRole("button", { name: "立即检查一次" }));
  await screen.findByText(/正在处理前台工作/);
  runResult = { status: "ok", backup_created: true };
  await user.click(screen.getByRole("button", { name: "立即检查一次" }));
  await screen.findByText(/已检查一次自动备份/);
  expect(
    requests.filter((r) => r.path.endsWith("/run-once")).length,
  ).toBe(2);
});
