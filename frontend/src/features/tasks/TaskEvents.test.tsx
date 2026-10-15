import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { TaskEvents } from "./TaskEvents";
import { backend, stamp } from "./test/fixtures";

const runningEvent = {
  id: 1,
  task_id: "task-1",
  type: "tool_result",
  data: {
    name: "run_command",
    result: {
      status: "running",
      execution: {
        session_id: "session-task",
        status: "running",
        cursor: 5,
        output: "start",
      },
    },
  },
  created_at: stamp,
};

test("任务事件刷新命令游标并保留累计输出", async () => {
  const { api, requests } = backend(({ path, method }) => {
    if (path.startsWith("/api/execution/sessions/session-task?") && method === "GET")
      return {
        session_id: "session-task",
        status: "success",
        cursor: 9,
        output: " done",
        result: { exit_code: 0 },
      };
  });
  const user = userEvent.setup();
  render(<TaskEvents api={api} taskId="task-1" events={[runningEvent]} />);
  await user.click(screen.getByText(/实际执行记录/));
  expect(screen.getByText("start")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "刷新命令状态" }));
  expect(await screen.findByText("start done")).toBeVisible();
  expect(screen.getByText(/命令状态：success/)).toBeVisible();
  expect(screen.getByText(/退出码 0/)).toBeVisible();
  expect(requests[0].path).toContain("run_id=task-1");
  expect(requests[0].path).toContain("kind=task");
  expect(requests[0].path).toContain("after=5");
});

test("任务事件可取消活动命令并显示最终输出", async () => {
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.endsWith("/cancel") && method === "POST") {
      expect(body).toEqual({ run_id: "task-1", kind: "task" });
      return {
        session_id: "session-task",
        status: "cancelled",
        cursor: 12,
        output: "start stopped",
        result: { exit_code: 1, cleanup_confirmed: true },
      };
    }
  });
  const user = userEvent.setup();
  render(<TaskEvents api={api} taskId="task-1" events={[runningEvent]} />);
  await user.click(screen.getByText(/实际执行记录/));
  await user.click(screen.getByRole("button", { name: "停止命令" }));
  expect(await screen.findByText("命令状态：cancelled · 退出码 1")).toBeVisible();
  expect(screen.getByText("start stopped")).toBeVisible();
  expect(requests).toHaveLength(1);
});
