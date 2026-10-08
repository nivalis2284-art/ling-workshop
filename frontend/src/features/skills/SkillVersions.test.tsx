import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { SkillsPage } from "./SkillsPage";
import { agent, backend, skill, stamp } from "../tasks/test/fixtures";

test("查看技能版本，确认后用目标版本和当前 revision 恢复", async () => {
  let saved = { ...skill, status: "active" };
  const { api, requests } = backend(({ path, body }) => {
    if (path.endsWith("/skills")) return [saved];
    if (path === "/api/skills/s1/versions")
      return [
        {
          skill_id: "s1",
          revision: 2,
          instructions: "旧版本的实际方法",
          status: "candidate",
          sources: skill.sources,
          created_at: stamp,
        },
      ];
    if (path === "/api/skills/s1/restore") {
      saved = {
        ...saved,
        revision: 5,
        status: "candidate",
        instructions: "旧版本的实际方法",
      };
      return saved;
    }
    throw new Error(path + String(body));
  });
  const user = userEvent.setup();
  render(<SkillsPage api={api} agent={agent} />);
  await user.click(await screen.findByText("查看技能版本"));
  await screen.findByText("旧版本的实际方法");
  await user.click(screen.getByRole("button", { name: "恢复到版本 2" }));
  expect(requests.some((r) => r.path.endsWith("/restore"))).toBe(false);
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认恢复技能",
    }),
  );
  await waitFor(() =>
    expect(requests.find((r) => r.path.endsWith("/restore"))?.body).toEqual({
      target_revision: 2,
      expected_revision: 4,
    }),
  );
  await screen.findByText(/版本 5/);
});

test("恢复遇到失效来源 409，显示冲突并重新读取，不伪报恢复", async () => {
  const { api, requests } = backend(({ path }) => {
    if (path.endsWith("/skills")) return [skill];
    if (path.endsWith("/versions"))
      return [
        {
          skill_id: "s1",
          revision: 1,
          instructions: "来源已失效的方法",
          status: "active",
          sources: skill.sources,
          created_at: stamp,
        },
      ];
    if (path.endsWith("/restore"))
      return Response.json({ detail: "技能来源已经失效" }, { status: 409 });
  });
  const user = userEvent.setup();
  render(<SkillsPage api={api} agent={agent} />);
  await user.click(await screen.findByText("查看技能版本"));
  await user.click(await screen.findByRole("button", { name: "恢复到版本 1" }));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认恢复技能",
    }),
  );
  await waitFor(() =>
    expect(
      within(screen.getByRole("dialog")).getByRole("alert"),
    ).toHaveTextContent(/资料已发生变化/),
  );
  expect(
    requests.filter((r) => r.path.endsWith("/skills")).length,
  ).toBeGreaterThan(1);
  expect(screen.queryByText("版本 5")).not.toBeInTheDocument();
});
