import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { LingApi } from "../../api/client";
import type {
  Conversation,
  ConversationMessage,
  SavedModel,
} from "../../api/types";
import { ChatHarness } from "../chat/testHarness";
import { useWorkEvents } from "./useWorkEvents";
import { WorkEventSummarySettings } from "./WorkEventSettings";

const profile: SavedModel = {
  id: "primary",
  name: "主用模型",
  credentials_available: true,
  profile: {
    api_family: "openai_responses",
    model: "fixture",
    base_url: "https://example.invalid",
    capabilities: {
      streaming: true,
      thinking: true,
      tools: false,
      effort_levels: [],
    },
  },
};

const node = (
  id: string,
  role: "user" | "assistant",
  text: string,
  parent_id: string | null,
): ConversationMessage => ({ id, role, text, parent_id });

/** 服务端事件行的夹具：字段名与 /work-events 返回一致。 */
function eventRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "e1",
    agent_id: "ling",
    conversation_id: "c1",
    title: "修复登录故障",
    status: "closed",
    lifecycle: "closed",
    outcome: "completed",
    folded: true,
    revision: 3,
    summary:
      "工作目标：修复登录故障；执行已结束，结果未全部核验；保留 4 条来源。",
    summary_kind: "rule",
    coverage: "retained",
    before_message_id: "a1",
    start_message_id: null,
    end_message_id: "a2",
    source_count: 4,
    message_ids: ["u2", "a2"],
    valid: true,
    reason: null,
    created_at: "2026-10-17T00:00:00Z",
    closed_at: "2026-10-17T01:00:00Z",
    ...overrides,
  };
}

function backend(
  options: {
    events?: unknown[];
    rejectStart?: { status: number; detail: string };
    rejectClose?: { status: number; detail: string };
    rejectFold?: { status: number; detail: string };
    rejectSummary?: { status: number; detail: string };
    settings?: {
      enabled: boolean;
      profile_id: string | null;
      max_calls: number;
    };
    jobs?: unknown[];
    records?: unknown;
    record?: unknown;
  } = {},
) {
  const requests: {
    path: string;
    method: string;
    body: Record<string, unknown> | null;
  }[] = [];
  let events = [...(options.events ?? [eventRow()])];
  let jobs = [...(options.jobs ?? [])];
  const conversations: Conversation[] = [
    {
      id: "c1",
      agent_id: "ling",
      title: "日常记录",
      scope: "real",
      head_id: "a2",
    },
    {
      id: "c2",
      agent_id: "ling",
      title: "另一段对话",
      scope: "real",
      head_id: "b1",
    },
  ];
  const paths: Record<string, ConversationMessage[]> = {
    a2: [
      node("u1", "user", "闲聊的开始", null),
      node("a1", "assistant", "闲聊的回复", "u1"),
      node("u2", "user", "帮我修一下登录", "a1"),
      node("a2", "assistant", "修好了", "u2"),
    ],
    b1: [node("b1", "user", "另一段对话的问题", null)],
  };
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });
  vi.stubGlobal("fetch", async (path: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path, method, body });
    if (path.endsWith("/main-conversation")) return json({ conversation: conversations[0], revision: 1, choices: conversations });
    if (path.endsWith("/work-branches")) return json([]);
    if (path === "/api/agents/ling/conversations") return json(conversations);
    if (path === "/api/conversations/c1/messages") return json(paths.a2);
    if (path === "/api/conversations/c2/messages") return json(paths.b1);
    if (path.endsWith("/branches"))
      return json([{ ...paths.a2.at(-1)!, selected: true }]);
    if (path === "/api/agents/ling/attachments") return json([]);
    if (path === "/api/models") return json([profile]);
    if (path === "/api/models/selection")
      return json({ primary: "primary", fallback: null });
    if (path === "/api/conversations/c1/access")
      return json({ conversation_id: "c1", mode: "confirm" });
    if (path === "/api/conversations/c2/access")
      return json({ conversation_id: "c2", mode: "confirm" });
    if (path === "/api/agents/ling/permissions")
      return json({
        agent_id: "ling",
        revision: 1,
        readable: [],
        writable: [],
      });
    if (path === "/api/agents/ling/work-event-settings")
      return json(
        options.settings ?? { enabled: false, profile_id: null, max_calls: 4 },
      );
    if (path === "/api/agents/ling/tasks") return json([]);
    if (path === "/api/conversations/c1/work-events") {
      if (method === "POST") {
        if (options.rejectStart)
          return json(
            { detail: options.rejectStart.detail },
            options.rejectStart.status,
          );
        const created = eventRow({
          id: "e2",
          title: body.title,
          status: "open",
          lifecycle: "open",
          outcome: "pending",
          folded: false,
          revision: 1,
          summary: "",
          coverage: "pending",
          source_count: 0,
        });
        events = [...events, created];
        return json(created, 201);
      }
      return json(events);
    }
    if (path === "/api/conversations/c2/work-events") return json([]);
    const one = path
      .split("?")[0]
      .match(/^\/api\/work-events\/([^/]+)(\/.*)?$/);
    if (one) {
      const id = one[1];
      const suffix = one[2] ?? "";
      const current = events.find(
        (item) => (item as { id: string }).id === id,
      ) as Record<string, unknown>;
      if (suffix === "/close") {
        if (options.rejectClose)
          return json(
            { detail: options.rejectClose.detail },
            options.rejectClose.status,
          );
        const closed = {
          ...current,
          status: "closed",
          lifecycle: "closed",
          outcome: "completed",
          folded: true,
          revision: (current.revision as number) + 1,
          coverage: "retained",
        };
        events = events.map((item) => (item === current ? closed : item));
        return json(closed);
      }
      if (suffix === "/fold") {
        if (options.rejectFold)
          return json(
            { detail: options.rejectFold.detail },
            options.rejectFold.status,
          );
        const folded = {
          ...current,
          folded: body.folded,
          revision: (current.revision as number) + 1,
        };
        events = events.map((item) => (item === current ? folded : item));
        return json(folded);
      }
      if (suffix === "/summary") {
        if (options.rejectSummary)
          return json(
            { detail: options.rejectSummary.detail },
            options.rejectSummary.status,
          );
        const updated = {
          ...current,
          summary: body.text,
          summary_kind: "owner",
          revision: (current.revision as number) + 1,
        };
        events = events.map((item) => (item === current ? updated : item));
        return json(updated);
      }
      if (suffix === "/tasks") return json(current);
      if (suffix === "/summarize") {
        const created = {
          id: "j" + (jobs.length + 1),
          status: "queued",
          outcome: null,
          error_code: null,
          coverage: null,
          usage: null,
          max_calls: 4,
          profile_id: "cheap",
          event_revision: current.revision,
          cancel_requested: 0,
        };
        jobs = [...jobs, created];
        return json(created, 202);
      }
      if (suffix === "/summary-jobs") return json(jobs);
      if (suffix.endsWith("/cancel")) {
        jobs = jobs.map((job) => ({
          ...(job as Record<string, unknown>),
          cancel_requested: 1,
        }));
        return json(jobs[0]);
      }
      if (suffix === "/records")
        return json(
          options.records ?? {
            event_id: id,
            revision: current.revision,
            items: [
              {
                id: "message:u2",
                source_id: "message:u2",
                kind: "message",
                location: "message:u2",
                coverage: "retained",
                byte_count: 12,
              },
            ],
            next_cursor: null,
            coverage: "retained",
          },
        );
      if (suffix.startsWith("/records/"))
        return json(
          options.record ?? {
            source_id: "message:u2",
            text: "帮我修一下登录",
            next_cursor: null,
            location: "message:u2",
            coverage: "retained",
            sha256: "a".repeat(64),
            byte_count: 12,
          },
        );
    }
    throw new Error("Unexpected HTTP endpoint: " + method + " " + path);
  });
  return {
    requests,
    events: () => events,
    jobs: () => jobs,
  };
}

function open() {
  const api = new LingApi("owner-fixture", () => {});
  return { ...render(<ChatHarness api={api} />), api };
}

test("聊天区间默认收起，本地展开与再次收起不改变输入策略或发出模型请求", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByRole("button", { name: "在界面展开这段工作的原始消息" });
  expect(screen.queryByText("修好了")).not.toBeInTheDocument();
  const before = server.requests.length;
  await user.click(
    screen.getByRole("button", { name: "在界面展开这段工作的原始消息" }),
  );
  expect(await screen.findByText("修好了")).toBeVisible();
  expect(screen.getByText("帮我修一下登录")).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "收起这段工作的原始消息" }),
  );
  expect(screen.queryByText("修好了")).not.toBeInTheDocument();
  expect(
    server.requests.slice(before).filter((item) => item.method !== "GET"),
  ).toEqual([]);
  expect(
    server.requests.slice(before).some((item) => item.path.includes("/chat")),
  ).toBe(false);
  expect(server.events()[0]).toMatchObject({ folded: true });
});

test("旧对话的延迟响应不能覆盖当前对话的事件列表", async () => {
  let resolveOld!: (value: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("/old/"))
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      return Promise.resolve(
        new Response(
          JSON.stringify([
            eventRow({ id: "new-event", conversation_id: "new" }),
          ]),
          { status: 200 },
        ),
      );
    }),
  );
  const api = new LingApi("owner-fixture", () => {});
  const hook = renderHook(({ cid }) => useWorkEvents(api, cid), {
    initialProps: { cid: "old" },
  });
  await waitFor(() => expect(resolveOld).toBeDefined());
  hook.rerender({ cid: "new" });
  await waitFor(() =>
    expect(hook.result.current.events?.[0]?.id).toBe("new-event"),
  );
  await act(async () => {
    resolveOld(
      new Response(JSON.stringify([eventRow({ id: "old-event" })]), {
        status: 200,
      }),
    );
  });
  expect(hook.result.current.events?.[0]?.id).toBe("new-event");
});

test("启用概况模型必须主动选择连接，保存设置本身不发起生成", async () => {
  const server = backend({
    settings: { enabled: false, profile_id: null, max_calls: 4 },
  });
  const user = userEvent.setup();
  render(
    <WorkEventSummarySettings
      api={new LingApi("owner-fixture", () => {})}
      agent={{ id: "ling", name: "灵" }}
    />,
  );
  const toggle = await screen.findByRole("checkbox", {
    name: /允许用所选模型/,
  });
  await user.click(toggle);
  expect(screen.getByRole("button", { name: "保存概况设置" })).toBeDisabled();
  await user.selectOptions(
    screen.getByLabelText("用于生成概况的连接"),
    "primary",
  );
  expect(screen.getByRole("button", { name: "保存概况设置" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "保存概况设置" }));
  await waitFor(() =>
    expect(
      server.requests.some(
        (item) =>
          item.method === "PUT" &&
          (item.body as { profile_id?: string })?.profile_id === "primary",
      ),
    ).toBe(true),
  );
  expect(server.requests.some((item) => item.path.includes("/summarize"))).toBe(
    false,
  );
});

async function panel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /工作事件/ }));
  return screen.getByRole("region", { name: "工作事件" });
}

test("开始工作用轻量标题，未结束前不显示完成/折叠入口", async () => {
  const server = backend({ events: [] });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  expect(within(view).getByText(/还没有工作事件/)).toBeVisible();
  await user.type(within(view).getByLabelText(/这次要做什么/), "修复登录故障");
  await user.click(within(view).getByRole("button", { name: "开始工作" }));
  const start = server.requests.find(
    (item) => item.method === "POST" && item.path.endsWith("/work-events"),
  )!;
  expect(start.body).toMatchObject({
    title: "修复登录故障",
    request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  // 未传起点：事件从当前 head 之后开始，不需要新开对话。
  expect(start.body).not.toHaveProperty("start_message_id");
  expect(await within(view).findByText("修复登录故障")).toBeVisible();
  expect(within(view).getByRole("button", { name: "结束工作" })).toBeEnabled();
  expect(
    within(view).queryByRole("button", { name: "折叠到模型输入" }),
  ).not.toBeInTheDocument();
  // 面板从不过滤或隐藏底层聊天记录：原文仍然在对话里。
  expect(screen.getByText("帮我修一下登录")).toBeVisible();
});

test("结束工作后按来源覆盖折叠或恢复原始输入，revision 随每次操作带出", async () => {
  const server = backend({
    events: [
      eventRow({
        folded: false,
        revision: 3,
        summary: "工作目标：修复登录故障。",
      }),
    ],
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  await user.click(
    within(view).getByRole("button", { name: "折叠到模型输入" }),
  );
  expect(
    server.requests.find((item) => item.path.endsWith("/work-events/e1/fold"))
      ?.body,
  ).toEqual({ expected_revision: 3, folded: true });
  expect(await within(view).findByText(/后续请求改用短概况/)).toBeVisible();
  await user.click(within(view).getByRole("button", { name: "恢复原始输入" }));
  expect(
    server.requests.filter((item) => item.path.endsWith("/fold")).at(-1)?.body,
  ).toEqual({ expected_revision: 4, folded: false });
});

test("未关闭的工作先关闭；服务端拒绝时如实显示原因且不谎报折叠", async () => {
  const server = backend({
    events: [
      eventRow({
        status: "open",
        lifecycle: "open",
        outcome: "pending",
        folded: false,
        revision: 1,
        summary: "",
        coverage: "pending",
      }),
    ],
    rejectClose: {
      status: 409,
      detail: "工作仍在进行、暂停或等待恢复，不能折叠。",
    },
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  await user.click(within(view).getByRole("button", { name: "结束工作" }));
  expect(await within(view).findByRole("alert")).toHaveTextContent(/仍在进行/);
  expect(within(view).getByRole("button", { name: "结束工作" })).toBeEnabled();
  expect(within(view).queryByText(/已折叠/)).not.toBeInTheDocument();
  expect(
    server.requests.filter((item) => item.path.endsWith("/close")),
  ).toHaveLength(1);
});

test("来源失效的事件只显示原因，没有折叠、更正或记录入口", async () => {
  backend({
    events: [
      eventRow({
        status: "stale",
        lifecycle: "closed",
        valid: false,
        reason: "sources_changed",
        folded: false,
      }),
    ],
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  expect(await within(view).findByText("来源已失效的工作")).toBeVisible();
  expect(within(view).getByText(/消息已被修改或删除/)).toBeVisible();
  expect(
    within(view).queryByRole("button", { name: /折叠到模型输入/ }),
  ).not.toBeInTheDocument();
  expect(
    within(view).queryByRole("button", { name: "查看记录" }),
  ).not.toBeInTheDocument();
});

test("查看记录只发 work-events 请求：目录分页与正文分页都不触发聊天或模型", async () => {
  const server = backend({
    records: {
      event_id: "e1",
      revision: 3,
      items: [
        {
          id: "message:u2",
          source_id: "message:u2",
          kind: "message",
          location: "message:u2",
          coverage: "retained",
          byte_count: 12,
        },
      ],
      next_cursor: "cursor-2",
      coverage: "retained",
    },
    record: {
      source_id: "message:u2",
      text: "第一页正文",
      next_cursor: "body-2",
      location: "message:u2",
      coverage: "retained",
      sha256: "b".repeat(64),
      byte_count: 300,
    },
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  const browsingStart = server.requests.length;
  await user.click(within(view).getByRole("button", { name: "查看记录" }));
  await user.click(
    await within(view).findByRole("button", { name: "读取记录目录" }),
  );
  expect(await within(view).findByText("message:u2")).toBeVisible();
  await user.click(within(view).getByRole("button", { name: "读取更多记录" }));
  await waitFor(() =>
    expect(
      server.requests.some((item) => item.path.endsWith("cursor-2&limit=20")),
    ).toBe(true),
  );
  await user.click(
    within(view).getAllByRole("button", { name: "打开正文" })[0],
  );
  expect(await within(view).findByText("第一页正文")).toBeVisible();
  await user.click(within(view).getByRole("button", { name: "读取下一页" }));
  await waitFor(() =>
    expect(
      server.requests.some(
        (item) =>
          item.path.includes("/records/message%3Au2") &&
          item.path.includes("cursor=body-2"),
      ),
    ).toBe(true),
  );
  // 关键断言：整段回读没有任何聊天或模型请求，也没有发送消息。
  expect(
    server.requests
      .slice(browsingStart)
      .some((item) => item.path.includes("/chat")),
  ).toBe(false);
  expect(
    server.requests
      .slice(browsingStart)
      .some((item) => item.path.includes("/models")),
  ).toBe(false);
  expect(
    server.requests.some(
      (item) => item.method !== "GET" && item.path.includes("/records"),
    ),
  ).toBe(false);
});

test("更正概况失败时不显示成功，也不发出服务端未确认的文本", async () => {
  backend({
    rejectSummary: {
      status: 409,
      detail: "工作事件版本已改变，请刷新后重试。",
    },
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  await user.click(within(view).getByRole("button", { name: "更正概况" }));
  await user.clear(within(view).getByLabelText("概况文本"));
  await user.type(within(view).getByLabelText("概况文本"), "我自己写的概况");
  await user.click(within(view).getByRole("button", { name: "保存更正" }));
  expect(await within(view).findByRole("alert")).toHaveTextContent(
    /版本已改变/,
  );
  expect(within(view).queryByText(/概况已更正/)).not.toBeInTheDocument();
  // 编辑框保留草稿，便于基于最新版本重试。
  expect(within(view).getByLabelText("概况文本")).toHaveValue("我自己写的概况");
});

test("概况更正成功后显示服务端确认的新概况", async () => {
  backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  await user.click(within(view).getByRole("button", { name: "更正概况" }));
  await user.clear(within(view).getByLabelText("概况文本"));
  await user.type(
    within(view).getByLabelText("概况文本"),
    "我手动更正为：登录故障已修复并核对。",
  );
  await user.click(within(view).getByRole("button", { name: "保存更正" }));
  expect(
    await within(view).findByText(/我手动更正为：登录故障已修复并核对。/),
  ).toBeVisible();
  expect(
    within(view).getByText(/模型输入：已折叠为概况 · 你的更正/),
  ).toBeVisible();
});

test("概况设置默认关闭且不自动使用主用模型；启用必须明确选择连接", async () => {
  const server = backend({
    settings: { enabled: false, profile_id: null, max_calls: 4 },
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  await user.click(within(view).getByRole("button", { name: "查看记录" }));
  // 未启用时没有生成入口。
  expect(
    within(view).queryByRole("button", { name: "用所选模型生成概况" }),
  ).not.toBeInTheDocument();
  expect(server.requests.some((item) => item.path.includes("/summarize"))).toBe(
    false,
  );
});

test("启用后可以请求生成、看到真实失败与调用上限，并取消队列中的作业", async () => {
  const server = backend({
    settings: { enabled: true, profile_id: "cheap", max_calls: 3 },
    jobs: [
      {
        id: "j1",
        status: "failed",
        outcome: null,
        error_code: "budget_exhausted",
        coverage: "capture_gap",
        usage: null,
        max_calls: 3,
        profile_id: "cheap",
        event_revision: 3,
        cancel_requested: 0,
      },
      {
        id: "j2",
        status: "queued",
        outcome: null,
        error_code: null,
        coverage: null,
        usage: null,
        max_calls: 3,
        profile_id: "cheap",
        event_revision: 3,
        cancel_requested: 0,
      },
    ],
  });
  const user = userEvent.setup();
  open();
  await screen.findByText("闲聊的开始");
  const view = await panel(user);
  await user.click(
    within(view).getByRole("button", { name: "用所选模型生成概况" }),
  );
  expect(
    server.requests.find((item) => item.path.endsWith("/summarize"))?.body,
  ).toEqual({ expected_revision: 3 });
  await user.click(within(view).getByRole("button", { name: "查看生成状态" }));
  expect(
    await within(view).findByText(/已达到本次整理的调用上限/),
  ).toBeVisible();
  expect(within(view).getAllByText(/服务商未返回用量/)).toHaveLength(
    server.jobs().length,
  );
  for (const label of within(view).getAllByText(/最多 3 次调用/))
    expect(label).toBeVisible();
  await user.click(
    within(view).getAllByRole("button", { name: "取消生成" })[0],
  );
  await waitFor(() =>
    expect(
      server.requests.some((item) =>
        item.path.endsWith("/summary-jobs/j2/cancel"),
      ),
    ).toBe(true),
  );
});

test("切到另一段对话不会带回上一段的事件、草稿或展开状态", async () => {
  backend();
  const user = userEvent.setup();
  const mounted = open();
  await screen.findByText("闲聊的开始");
  const first = await panel(user);
  await user.type(
    within(first).getByLabelText(/这次要做什么/),
    "还没提交的草稿",
  );
  await user.click(within(first).getByRole("button", { name: "更正概况" }));
  expect(within(first).getByLabelText("概况文本")).toBeVisible();
  mounted.unmount();
  // 事件从会话选择切到 c2。
  open();
  await user.click(await screen.findByRole("button", { name: "历史与旧会话" }));
  const history = screen.getByRole("dialog", { name: "历史与旧会话" });
  await user.click(within(history).getByRole("button", { name: /c2/ }));
  await screen.findByText("另一段对话的问题");
  const second = await panel(user);
  expect(within(second).queryByText("修复登录故障")).not.toBeInTheDocument();
  expect(within(second).queryByText("还没提交的草稿")).not.toBeInTheDocument();
  expect(within(second).queryByLabelText("概况文本")).not.toBeInTheDocument();
  expect(within(second).getByText(/还没有工作事件/)).toBeVisible();
});
