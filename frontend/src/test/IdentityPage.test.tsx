import { expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../api/client";
import { IdentityPage } from "../features/identity/IdentityPage";

const agent = { id: "ling-1", name: "小灵" };
const ref = { id: "source-1", revision: 1 };
const pending = {
  id: "proposal-1",
  key: "relationship.owner",
  text: "把所有者当作长期合作的人。",
  status: "awaiting_owner",
  sources: [ref],
  base_revision: 0,
  kind: "relationship",
};

function backend(
  options: { entries?: { key: string; text: string; kind: string }[] } = {},
) {
  let revision = 0;
  let rules = ["始终使用中文回复"];
  let customized = false;
  let entries = options.entries ?? [];
  let status = "awaiting_owner";
  const requests: {
    url: URL;
    method: string;
    body: Record<string, unknown> | null;
  }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, "http://localhost");
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      requests.push({ url, method, body });
      const json = (value: unknown, code = 200) =>
        new Response(JSON.stringify(value), {
          status: code,
          headers: { "Content-Type": "application/json" },
        });
      const view = {
        agent_id: agent.id,
        entries: entries.map((entry) => ({
          ...entry,
          origin: { proposal_id: "proposal-1", kind: entry.kind, sources: [ref] },
        })),
      };
      const instructions = {
        agent_id: agent.id,
        rules,
        defaults: ["程序自带的一条"],
        customized,
      };
      if (url.pathname.endsWith("/instructions") && method === "PUT") {
        rules = body.rules as string[];
        customized = true;
        return json(instructions);
      }
      if (url.pathname.endsWith("/instructions/reset")) {
        rules = ["程序自带的一条"];
        customized = false;
        return json(instructions);
      }
      if (url.pathname.endsWith("/instructions")) return json(instructions);
      if (url.pathname.endsWith("/entries/remove")) {
        revision += 1;
        entries = entries.filter((entry) => entry.key !== body.key);
        return json({ agent_id: agent.id, revision, entries: view.entries });
      }
      if (url.pathname.endsWith("/entries") && method === "POST") {
        revision += 1;
        entries = [
          ...entries,
          {
            key: body.key as string,
            text: body.text as string,
            kind: body.kind as string,
          },
        ];
        return json({ agent_id: agent.id, revision, entries: view.entries }, 201);
      }
      if (url.pathname.endsWith("/entries")) return json(view);
      if (url.pathname.endsWith("/state"))
        return json({
          agent_id: agent.id,
          revision,
          entries: Object.fromEntries(
            entries.map((entry) => [entry.key, entry.text]),
          ),
        });
      if (url.pathname.endsWith("/proposals"))
        return json(status === "awaiting_owner" ? [{ ...pending, status }] : []);
      if (url.pathname.endsWith("/revisions"))
        return json([
          { revision, entries: {}, reason: "initialize", created_at: null },
        ]);
      if (url.pathname.endsWith("/decision")) {
        status = body.decision === "approve" ? "applied_owner" : "rejected";
        return json({ ...pending, status });
      }
      throw new Error(`Unexpected request: ${method} ${url.pathname}`);
    }),
  );
  return requests;
}

function open(options?: Parameters<typeof backend>[0]) {
  const requests = backend(options);
  render(<IdentityPage api={new LingApi("token", () => {})} agent={agent} />);
  return requests;
}

test("七个部分默认折叠，只显示名称和条数", async () => {
  open();
  const section = await screen.findByRole("region", { name: "当前身份" });
  for (const name of [
    "身份理解",
    "性格",
    "习惯",
    "关系",
    "价值观",
    "能力",
    "自定义条目",
  ])
    expect(within(section).getByText(name)).toBeInTheDocument();
  expect(within(section).getAllByText("0 条")).toHaveLength(7);
  vi.unstubAllGlobals();
});

test("改成自己的指令后，程序自带的那几条仍然查得到、也能恢复", async () => {
  const requests = open();
  await screen.findByText("始终使用中文回复");
  await userEvent.click(
    screen.getByRole("button", { name: "修改第 1 条指令" }),
  );
  const box = screen.getByLabelText("指令内容");
  await userEvent.clear(box);
  await userEvent.type(box, "只讲重点");
  await userEvent.click(screen.getByRole("button", { name: "保存这条" }));
  const section = await screen.findByRole("region", { name: "系统指令" });
  await userEvent.click(
    within(section).getByText("程序自带的 1 条（内容参考，不生效中）"),
  );
  expect(within(section).getByText("程序自带的一条")).toBeInTheDocument();
  await userEvent.click(
    within(section).getByRole("button", { name: /恢复成这几条/ }),
  );
  await userEvent.click(
    within(section).getByRole("button", { name: "确认恢复" }),
  );
  await waitFor(() =>
    expect(
      requests.some((item) => item.url.pathname.endsWith("/instructions/reset")),
    ).toBe(true),
  );
  vi.unstubAllGlobals();
});

test("改一条系统指令会原样写回清单", async () => {
  const requests = open();
  await screen.findByText("始终使用中文回复");
  await userEvent.click(
    screen.getByRole("button", { name: "修改第 1 条指令" }),
  );
  const box = screen.getByLabelText("指令内容");
  await userEvent.clear(box);
  await userEvent.type(box, "始终使用中文，并先给结论");
  await userEvent.click(screen.getByRole("button", { name: "保存这条" }));
  await waitFor(() =>
    expect(
      requests.some((item) => item.method === "PUT"),
    ).toBe(true),
  );
  expect(
    requests.find((item) => item.method === "PUT")?.body,
  ).toEqual({ rules: ["始终使用中文，并先给结论"] });
  vi.unstubAllGlobals();
});

test("删掉最后一条指令会写出空清单，并提示灵只收到名称和状态", async () => {
  const requests = open();
  await screen.findByText("始终使用中文回复");
  await userEvent.click(screen.getByRole("button", { name: "删除第 1 条指令" }));
  await waitFor(() =>
    expect(requests.find((item) => item.method === "PUT")?.body).toEqual({
      rules: [],
    }),
  );
  expect(
    await screen.findByText(/现在一条指令都没有/),
  ).toBeInTheDocument();
  vi.unstubAllGlobals();
});

test("在指定部分里添加条目，条目名会带上该部分的前缀", async () => {
  const requests = open();
  const section = await screen.findByRole("region", { name: "当前身份" });
  await userEvent.click(
    within(section).getByRole("button", { name: "在关系里添加条目" }),
  );
  await userEvent.type(screen.getByLabelText("条目名字"), "与所有者的相处方式");
  await userEvent.type(
    screen.getByLabelText("内容"),
    "把所有者当成长期合作的人。",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() =>
    expect(
      requests.some(
        (item) =>
          item.url.pathname.endsWith("/entries") && item.method === "POST",
      ),
    ).toBe(true),
  );
  expect(
    requests.find(
      (item) =>
        item.url.pathname.endsWith("/entries") && item.method === "POST",
    )?.body,
  ).toMatchObject({
    kind: "relationship",
    key: "relationship.与所有者的相处方式",
    text: "把所有者当成长期合作的人。",
    expected_revision: 0,
  });
  vi.unstubAllGlobals();
});

test("条目名字留空时不提交，并说明原因", async () => {
  const requests = open();
  const section = await screen.findByRole("region", { name: "当前身份" });
  await userEvent.click(
    within(section).getByRole("button", { name: "在性格里添加条目" }),
  );
  await userEvent.type(screen.getByLabelText("内容"), "说话别绕弯");
  await userEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "给这条起个名字",
  );
  expect(
    requests.filter(
      (item) =>
        item.url.pathname.endsWith("/entries") && item.method === "POST",
    ),
  ).toHaveLength(0);
  vi.unstubAllGlobals();
});

test("删条目会带上当前版本号，不会盲目覆盖", async () => {
  const requests = open({
    entries: [{ key: "identity.name", text: "小灵", kind: "identity" }],
  });
  const section = await screen.findByRole("region", { name: "当前身份" });
  await userEvent.click(
    within(section).getByRole("button", { name: "删除 当前名字" }),
  );
  await waitFor(() =>
    expect(
      requests.find((item) =>
        item.url.pathname.endsWith("/entries/remove"),
      )?.body,
    ).toEqual({ key: "identity.name", expected_revision: 0 }),
  );
  vi.unstubAllGlobals();
});

test("自由命名的条目在界面上显示自己起的名字，不被吞掉", async () => {
  open({
    entries: [{ key: "custom.咖啡偏好", text: "手冲，不加糖。", kind: "custom" }],
  });
  const section = await screen.findByRole("region", { name: "当前身份" });
  await userEvent.click(within(section).getByText("自定义条目"));
  expect(within(section).getByText("咖啡偏好")).toBeInTheDocument();
  expect(within(section).getByText("手冲，不加糖。")).toBeInTheDocument();
  vi.unstubAllGlobals();
});

test("待审批按部分归类，只给通过或不通过两个动作", async () => {
  const requests = open();
  const section = await screen.findByRole("region", { name: "待审批" });
  expect(within(section).getByText("1 条待你决定")).toBeInTheDocument();
  await userEvent.click(within(section).getAllByText("关系")[0]);
  await userEvent.click(
    within(section).getByRole("button", { name: "批准这项变化" }),
  );
  await waitFor(() =>
    expect(
      requests.find((item) => item.url.pathname.endsWith("/decision"))?.body,
    ).toEqual({ decision: "approve", expected_revision: 0 }),
  );
  vi.unstubAllGlobals();
});
