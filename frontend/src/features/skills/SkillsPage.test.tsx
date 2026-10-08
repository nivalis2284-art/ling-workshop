import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { SkillsPage } from "./SkillsPage";
import { agent, backend, skill } from "../tasks/test/fixtures";

test("技能展示原始来源版本，启用与停用发送当前 revision", async () => {
  let saved = { ...skill };
  const { api, requests } = backend(({ path, method, body }) => {
    if (path.endsWith("/skills")) return [saved];
    if (path === "/api/sources/source-1?revision=2")
      return {
        id: "source-1",
        revision: 2,
        text: "实际方法依据",
        kind: "tool",
        scope: "real",
      };
    if (path === "/api/skills/s1/decision" && method === "POST") {
      saved = {
        ...saved,
        revision: saved.revision + 1,
        status: body.action === "activate" ? "active" : "disabled",
      };
      return saved;
    }
  });
  const user = userEvent.setup();
  render(<SkillsPage api={api} agent={agent} />);
  await user.click(
    await screen.findByRole("button", { name: "查看来源 1，版本 2" }),
  );
  await screen.findByText("实际方法依据");
  await user.click(screen.getByRole("button", { name: "启用技能" }));
  await screen.findByText("已启用");
  expect(requests.find((r) => r.method === "POST")?.body).toEqual({
    action: "activate",
    expected_revision: 4,
  });
  await user.click(screen.getByRole("button", { name: "停用技能" }));
  await waitFor(() =>
    expect(requests.filter((r) => r.method === "POST").at(-1)?.body).toEqual({
      action: "disable",
      expected_revision: 5,
    }),
  );
});
