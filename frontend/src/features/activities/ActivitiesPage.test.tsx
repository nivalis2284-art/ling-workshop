import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { ActivitiesPage } from "./ActivitiesPage";
import { activity, agent, backend } from "../tasks/test/fixtures";

test("网页关注先精确授权，再创建；默认间隔为十五分钟，通知来自服务", async () => {
  let origins = { agent_id: agent.id, revision: 0, origins: [] as string[] };
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.endsWith("/goals")) return [];
    if (path.includes("/activity-web-origins")) {
      if (method === "PUT")
        origins = {
          agent_id: agent.id,
          revision: 1,
          origins: body.origins as string[],
        };
      return origins;
    }
    if (path.includes("/activity-notifications?")) return [];
    if (path.includes("/activities?")) return [];
    if (path === "/api/activities" && method === "POST")
      return { ...activity, ...body, kind: "web_watch" };
  });
  const user = userEvent.setup();
  render(<ActivitiesPage api={api} agent={agent} />);
  await user.selectOptions(
    await screen.findByLabelText("活动类型"),
    "web_watch",
  );
  await user.type(screen.getByLabelText("活动标题"), "关注发布说明");
  await user.type(
    screen.getByLabelText("关注目标"),
    "https://example.com/releases",
  );
  await user.click(screen.getByRole("button", { name: "创建活动" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("授权");
  expect(requests.some((r) => r.path === "/api/activities")).toBe(false);
  await user.type(
    screen.getByLabelText("允许关注的网站来源"),
    "https://example.com",
  );
  await user.click(screen.getByRole("button", { name: "保存网页关注授权" }));
  await screen.findByText("网页关注授权已保存。");
  await user.click(screen.getByRole("button", { name: "创建活动" }));
  await waitFor(() =>
    expect(requests.some((r) => r.path === "/api/activities")).toBe(true),
  );
  expect(requests.find((r) => r.method === "PUT")?.body).toEqual({
    origins: ["https://example.com"],
    expected_revision: 0,
  });
  expect(
    requests.find((r) => r.path === "/api/activities")?.body,
  ).toMatchObject({
    agent_id: "a/1",
    kind: "web_watch",
    title: "关注发布说明",
    target: "https://example.com/releases",
    interval_seconds: 900,
  });
  expect(screen.getByText("还没有活动通知")).toBeInTheDocument();
});

test("活动暂停和恢复携带最新 revision，取消经确认", async () => {
  let saved = { ...activity };
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.endsWith("/goals")) return [];
    if (path.includes("/activities?")) return [saved];
    if (path.includes("/activity-notifications?"))
      return [
        {
          id: "n1",
          activity_id: "ac1",
          agent_id: agent.id,
          evidence_id: "e1",
          message: "实际提醒消息",
          created_at: activity.created_at,
        },
      ];
    if (path.endsWith("/activity-web-origins"))
      return { agent_id: agent.id, revision: 0, origins: [] };
    if (
      path === "/api/activities/ac1/pause" ||
      path === "/api/activities/ac1/cancel" ||
      (path === "/api/activities/ac1" && method === "PATCH")
    ) {
      saved = {
        ...saved,
        revision: saved.revision + 1,
        status: path.endsWith("pause")
          ? "paused"
          : path.endsWith("cancel")
            ? "cancelled"
            : String(body.status),
      };
      return saved;
    }
  });
  const user = userEvent.setup();
  render(<ActivitiesPage api={api} agent={agent} />);
  await screen.findByText("实际提醒消息");
  await user.click(screen.getByRole("button", { name: "暂停活动" }));
  await user.click(await screen.findByRole("button", { name: "恢复活动" }));
  expect(requests.find((r) => r.method === "PATCH")?.body).toEqual({
    expected_revision: 4,
    status: "active",
  });
  await user.click(await screen.findByRole("button", { name: "取消活动" }));
  expect(requests.some((r) => r.path.endsWith("/cancel"))).toBe(false);
  await user.click(screen.getByRole("button", { name: "确认取消活动" }));
  await waitFor(() =>
    expect(requests.find((r) => r.path.endsWith("/cancel"))?.body).toEqual({
      expected_revision: 5,
    }),
  );
});
