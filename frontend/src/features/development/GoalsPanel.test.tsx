import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { GoalsPanel } from "./GoalsPanel";
import { agent, backend, stamp } from "../tasks/test/fixtures";

test("长期目标先记录真实来源，提交为候选，显式启用并只读展示进展", async () => {
  let goals: {
    id: string;
    agent_id: string;
    title: string;
    status: string;
    progress: string;
    revision: number;
    sources: { id: string; revision: number }[];
    created_at: string;
  }[] = [];
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.endsWith("/goals") && method === "GET") return goals;
    if (path === "/api/sources")
      return { id: "real-owner-source", revision: 7 };
    if (path === "/api/goals") {
      goals = [
        {
          id: "g1",
          agent_id: agent.id,
          title: String(body.title),
          status: "proposed",
          progress: "",
          revision: 1,
          sources: body.sources as (typeof goals)[number]["sources"],
          created_at: stamp,
        },
      ];
      return goals[0];
    }
    if (path === "/api/goals/g1/decision") {
      goals = [
        {
          ...goals[0],
          revision: goals[0].revision + 1,
          status: {
            activate: "active",
            pause: "paused",
            complete: "completed",
            abandon: "abandoned",
          }[String(body.action)]!,
          progress: "已核对两份实际资料",
        },
      ];
      return goals[0];
    }
  });
  const user = userEvent.setup();
  render(<GoalsPanel api={api} agent={agent} />);
  await user.type(
    await screen.findByLabelText("长期目标标题"),
    "持续整理项目知识",
  );
  await user.click(screen.getByRole("button", { name: "提出长期目标" }));
  await screen.findByText("待启用");
  const writes = requests.filter((request) => request.method === "POST");
  expect(writes.map((request) => request.path)).toEqual([
    "/api/sources",
    "/api/goals",
  ]);
  expect(writes[0].body).toEqual({
    agent_id: "a/1",
    content: "长期目标：持续整理项目知识",
    request_id: expect.any(String),
  });
  expect(writes[1].body).toEqual({
    agent_id: "a/1",
    title: "持续整理项目知识",
    sources: [{ id: "real-owner-source", revision: 7 }],
    request_id: expect.any(String),
  });
  expect(requests.some((r) => r.path.endsWith("/decision"))).toBe(false);
  await user.click(screen.getByRole("button", { name: "启用目标" }));
  await screen.findByText("已核对两份实际资料");
  expect(
    screen.queryByRole("textbox", { name: /进展/ }),
  ).not.toBeInTheDocument();
  expect(requests.find((r) => r.path.endsWith("/decision"))?.body).toEqual({
    action: "activate",
    expected_revision: 1,
  });
  await user.click(screen.getByRole("button", { name: "暂停目标" }));
  await user.click(await screen.findByRole("button", { name: "恢复目标" }));
  await user.click(await screen.findByRole("button", { name: "标记目标完成" }));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认完成目标",
    }),
  );
  await waitFor(() =>
    expect(
      requests.filter((r) => r.path.endsWith("/decision")).at(-1)?.body,
    ).toEqual({ action: "complete", expected_revision: 4 }),
  );
});

test("来源记录失败时不提交目标，重试复用请求编号", async () => {
  let failure = true;
  const { api, requests } = backend(({ path, method }) => {
    if (path.endsWith("/goals") && method === "GET") return [];
    if (path === "/api/sources") {
      if (failure)
        return Response.json({ detail: "记录未保存" }, { status: 503 });
      return { id: "saved-source", revision: 1 };
    }
    if (path === "/api/goals") return { id: "g2", status: "proposed" };
  });
  const user = userEvent.setup();
  render(<GoalsPanel api={api} agent={agent} />);
  await user.type(await screen.findByLabelText("长期目标标题"), "维护知识");
  await user.click(screen.getByRole("button", { name: "提出长期目标" }));
  await screen.findByRole("alert");
  expect(requests.some((r) => r.path === "/api/goals")).toBe(false);
  failure = false;
  await user.click(screen.getByRole("button", { name: "提出长期目标" }));
  await screen.findByText("目标已提出，请在列表中核对来源并启用。");
  expect(
    requests.filter((r) => r.path === "/api/sources")[0].body.request_id,
  ).toBe(requests.filter((r) => r.path === "/api/sources")[1].body.request_id);
});
