import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ChatPage } from "./ChatPage";
import { ChatSessions } from "./session";
import { LingApi } from "../../api/client";
import type {
  ConversationMessage,
  Conversation,
  SavedModel,
} from "../../api/types";

/** The model picker now lives in a popup at the composer's bottom-right. */
async function chooseModel(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
) {
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  await user.click(screen.getByRole("radio", { name }));
}

async function chooseEffort(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
) {
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  await user.click(screen.getByRole("radio", { name: label }));
}

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
      effort_levels: ["low", "high"],
    },
  },
};
const node = (
  id: string,
  role: "user" | "assistant",
  text: string,
  parent_id: string | null,
): ConversationMessage => ({
  id,
  role,
  text,
  parent_id,
  source: { id: "source-" + id, revision: 2 },
});
const original = [
  node("u1", "user", "原问题", null),
  node("a1", "assistant", "原回复", "u1"),
];

function backend(
  options: {
    empty?: boolean;
    failMessages?: boolean;
    delayMessages?: boolean;
  } = {},
) {
  const requests: {
    path: string;
    body: Record<string, unknown> | null;
    init: RequestInit;
  }[] = [];
  const conversations: Conversation[] = options.empty
    ? []
    : [
        {
          id: "c1",
          agent_id: "ling",
          title: "日常记录",
          scope: "real",
          head_id: "a1",
        },
        {
          id: "c2",
          agent_id: "ling",
          title: "海岛故事",
          scope: "scene:island",
          head_id: "u2",
        },
      ];
  const paths: Record<string, ConversationMessage[]> = {
    a1: original,
    u2: [node("u2", "user", "岛上的问题", null)],
  };
  let stream: ReadableStreamDefaultController<Uint8Array>;
  let currentRequest: Record<string, unknown> = {};
  let currentUser: ConversationMessage | undefined;
  let runStatus = "running";
  let failMessages = !!options.failMessages;
  let releaseMessages: (() => void) | undefined;
  const gate = options.delayMessages
    ? new Promise<void>((resolve) => {
        releaseMessages = resolve;
      })
    : Promise.resolve();
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer owner-fixture",
    );
    expect(init.cache).toBe("no-store");
    const body = init.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path, body, init });
    if (path === "/api/models")
      return json([
        profile,
        {
          ...profile,
          id: "backup",
          name: "备用模型",
          profile: {
            ...profile.profile,
            capabilities: { streaming: true, thinking: false, tools: false },
          },
        },
      ]);
    if (path === "/api/models/selection")
      return json({ primary: "primary", fallback: "backup" });
    if (path === "/api/agents/ling/conversations") return json(conversations);
    if (path === "/api/agents/ling/attachments") return json([]);
    if (path === "/api/conversations") {
      const value: Conversation = {
        id: "created",
        agent_id: "ling",
        title: body.title,
        scope: body.scene
          ? "scene:" +
            (/^[A-Za-z0-9_-]+$/.test(body.scene)
              ? body.scene
              : "fixture-scene-uuid")
          : "real",
        ...(body.scene ? { background: body.background ?? "" } : {}),
        head_id: null,
      };
      conversations.unshift(value);
      return json(value, 201);
    }
    const match = path.match(
      /^\/api\/conversations\/([^/]+)\/(messages|branches|edit|head)$/,
    );
    if (match) {
      const value = conversations.find((c) => c.id === match[1])!;
      if (match[2] === "messages") {
        const snapshot = paths[value.head_id ?? ""] ?? [];
        if (value.id === "c1") await gate;
        return failMessages ? json({ detail: "offline" }, 503) : json(snapshot);
      }
      if (match[2] === "branches")
        return json(
          Object.values(paths)
            .filter(
              (p) =>
                p.length &&
                (value.id === "c1"
                  ? p[0].id !== "u2"
                  : value.id === "c2"
                    ? p[0].id === "u2"
                    : false),
            )
            .map((p) => ({
              ...p.at(-1)!,
              selected: p.at(-1)!.id === value.head_id,
            })),
        );
      if (match[2] === "edit") {
        const edited = node("edited", "user", body.content, null);
        paths.edited = [edited];
        value.head_id = edited.id;
        return json(edited);
      }
      value.head_id = body.message_id;
      return json(value);
    }
    if (path === "/api/chat/runs/run")
      return json({
        id: "run",
        agent_id: "ling",
        request_id: currentRequest.request_id,
        conversation_id: currentRequest.conversation_id,
        user_message_id: currentUser?.id,
        profile_id: currentRequest.profile_id,
        status: runStatus,
        user_source: currentUser?.source,
        assistant_source: null,
        context_sources: [],
        created_at: "2026-10-09T00:00:00Z",
        finished_at: null,
      });
    if (path === "/api/chat/stream") {
      currentRequest = body;
      const value = conversations.find((c) => c.id === body.conversation_id)!;
      const selected = paths[value.head_id ?? ""] ?? [];
      runStatus = "running";
      if (body.regenerate_message_id) {
        currentUser = selected.find(
          (message) => message.id === body.regenerate_message_id,
        );
        expect(currentUser?.role).toBe("user");
        expect(body.message).toBe(currentUser?.text);
        paths[currentUser!.id] = selected.slice(
          0,
          selected.indexOf(currentUser!) + 1,
        );
      } else {
        currentUser = node(
          "user-" + body.request_id,
          "user",
          body.message,
          value.head_id,
        );
        paths[currentUser.id] = [...selected, currentUser];
      }
      value.head_id = currentUser!.id;
    }
    if (path === "/api/chat/stream" || path.includes("/events?"))
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            stream = controller;
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    throw new Error("Unexpected HTTP endpoint: " + path);
  });
  return {
    requests,
    paths,
    conversations,
    releaseMessages,
    recover() {
      failMessages = false;
    },
    async emit(event: string, data: Record<string, unknown>, id: number) {
      if (event === "error")
        runStatus = data.code === "cancelled" ? "cancelled" : "failed";
      if (event === "complete") runStatus = "completed";
      await act(async () => {
        stream.enqueue(
          new TextEncoder().encode(
            "id: " +
              id +
              "\nevent: " +
              event +
              "\ndata: " +
              JSON.stringify({ run_id: "run", ...data }) +
              "\n\n",
          ),
        );
      });
    },
  };
}
function open() {
  const api = new LingApi("owner-fixture", () => {});
  const sessions = new ChatSessions(api);
  return {
    ...render(
      <ChatPage
        api={api}
        agent={{ id: "ling", name: "灵" }}
        session={sessions.forAgent("ling")}
        openSettings={() => {}}
      />,
    ),
    sessions,
  };
}

test("backend transcripts survive fresh browser storage and conversations of the same agent stay isolated", async () => {
  const server = backend();
  const user = userEvent.setup();
  const view = open();
  expect(await screen.findByText("原回复")).toBeVisible();
  expect(screen.getByLabelText("保存的对话")).toHaveValue("c1");
  expect(screen.queryByText(/当前浏览器会话/)).not.toBeInTheDocument();
  expect(view.sessions.forAgent("ling", "c1")).not.toBe(
    view.sessions.forAgent("ling", "c2"),
  );
  await user.type(screen.getByLabelText("消息"), "日常草稿");
  await user.selectOptions(screen.getByLabelText("保存的对话"), "c2");
  expect(await screen.findByText("岛上的问题")).toBeVisible();
  expect(screen.queryByText("原回复")).not.toBeInTheDocument();
  expect(screen.getByLabelText("消息")).toHaveValue("");
  await user.selectOptions(screen.getByLabelText("保存的对话"), "c1");
  expect(await screen.findByText("原回复")).toBeVisible();
  expect(screen.getByLabelText("消息")).toHaveValue("日常草稿");
  view.unmount();
  sessionStorage.clear();
  open();
  expect(await screen.findByText("原回复")).toBeVisible();
  expect(
    server.requests.filter((r) => r.path.endsWith("/c1/messages")).length,
  ).toBeGreaterThanOrEqual(3);
});

test("editing a user creates a branch, old branch selection restores source IDs, and assistants cannot be edited", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  const log = screen.getByRole("log");
  expect(within(log).getAllByRole("button", { name: "编辑消息" })).toHaveLength(
    1,
  );
  expect(
    within(screen.getByRole("article", { name: "灵的回复" })).queryByRole(
      "button",
      { name: "编辑消息" },
    ),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "编辑消息" }));
  await user.clear(screen.getByLabelText("编辑你的消息"));
  await user.type(screen.getByLabelText("编辑你的消息"), "修改的问题");
  await user.click(screen.getByRole("button", { name: "保存为新分支" }));
  expect(await screen.findByText("修改的问题")).toBeVisible();
  expect(screen.queryByText("原回复")).not.toBeInTheDocument();
  expect(server.requests.find((r) => r.path.endsWith("/edit"))?.body).toEqual({
    message_id: "u1",
    content: "修改的问题",
    request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  expect(screen.getByLabelText("对话分支")).toHaveValue("edited");
  await user.selectOptions(screen.getByLabelText("对话分支"), "a1");
  expect(await screen.findByText("原回复")).toBeVisible();
  expect(server.requests.find((r) => r.path.endsWith("/head"))?.body).toEqual({
    message_id: "a1",
  });
  expect(screen.getByRole("article", { name: "你的消息" })).toHaveAttribute(
    "data-message-id",
    "u1",
  );
  expect(screen.getByRole("article", { name: "你的消息" })).toHaveAttribute(
    "data-source-id",
    "source-u1",
  );
});

test("regenerate reuses exact parent user text and reads the committed replacement without duplicating the user", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  server.paths.a1 = [node("u1", "user", "  原问题\n", null), original[1]];
  await screen.findByText("原回复");
  await chooseEffort(user, "high");
  await user.click(screen.getByRole("button", { name: "重新生成" }));
  const body = server.requests.find((r) => r.path === "/api/chat/stream")!.body;
  expect(body).toEqual({
    agent_id: "ling",
    conversation_id: "c1",
    message: "  原问题\n",
    regenerate_message_id: "u1",
    profile_id: "primary",
    effort: "high",
    request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  expect(screen.getByRole("button", { name: "重新生成" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "编辑消息" })).toBeDisabled();
  expect(screen.getByLabelText("保存的对话")).toBeDisabled();
  await server.emit("started", { conversation_id: "c1" }, 1);
  await server.emit("text", { text: "新回复" }, 2);
  const replacement = node("a-new", "assistant", "后端确认的新回复", "u1");
  server.paths[replacement.id] = [server.paths.a1[0], replacement];
  server.conversations[0].head_id = replacement.id;
  await server.emit(
    "complete",
    { status: "completed", assistant_source: replacement.source },
    3,
  );
  expect(await screen.findByText("后端确认的新回复")).toBeVisible();
  expect(screen.queryByText("原回复")).not.toBeInTheDocument();
  expect(screen.queryByText("新回复")).not.toBeInTheDocument();
  expect(
    within(screen.getByRole("log")).getAllByRole("article", {
      name: "你的消息",
    }),
  ).toHaveLength(1);
  expect(screen.getByRole("article", { name: "灵的回复" })).toHaveAttribute(
    "data-source-id",
    "source-a-new",
  );
  expect(screen.getByLabelText("对话分支")).toHaveValue("a-new");
});

test("model restart removes old reply and thinking, announces backup, and a replayed switch cannot clear new text", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  await user.type(screen.getByLabelText("消息"), "下一条");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  await server.emit("started", { conversation_id: "c1" }, 1);
  await server.emit("thinking", { text: "旧思考" }, 2);
  await server.emit("text", { text: "旧草稿" }, 3);
  await server.emit(
    "notice",
    { code: "model_unavailable", message: "fixture-provider-detail" },
    4,
  );
  await server.emit(
    "model_switch",
    { from_profile: "primary", to_profile: "backup", restart: true },
    5,
  );
  expect(screen.queryByText("旧草稿")).not.toBeInTheDocument();
  expect(screen.queryByText("旧思考")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/备用.*重新/);
  await server.emit(
    "notice",
    {
      code: "effort_omitted",
      message: "fixture-effort-detail",
      profile_id: "backup",
      requested_effort: "high",
    },
    6,
  );
  expect(screen.getByRole("status")).toHaveTextContent(/默认设置/);
  expect(document.body.textContent).not.toContain("fixture-provider-detail");
  expect(Object.values(sessionStorage).join(" ")).not.toContain(
    "fixture-effort-detail",
  );
  await server.emit("text", { text: "备用正文" }, 7);
  await server.emit(
    "model_switch",
    { from_profile: "primary", to_profile: "backup", restart: true },
    5,
  );
  expect(screen.getByText("备用正文")).toBeVisible();
});

test.each(["natural", "scene"])(
  "creates an explicit %s conversation and posts only current input, conversation and model options",
  async (kind) => {
    const server = backend({ empty: true });
    const user = userEvent.setup();
    open();
    await user.click(await screen.findByRole("button", { name: "新建对话" }));
    await user.type(screen.getByLabelText("对话名称"), "新交流");
    if (kind === "scene") {
      await user.selectOptions(screen.getByLabelText("对话类型"), "scene");
      await user.type(screen.getByLabelText("场景名称"), "海岛相遇");
      expect(screen.getByLabelText("场景背景")).toHaveAttribute(
        "maxlength",
        "6000",
      );
      await user.type(
        screen.getByLabelText("场景背景"),
        "我们在虚构的海岛上探索。\n你是同行的伙伴。",
      );
    }
    await user.click(screen.getByRole("button", { name: "创建对话" }));
    await waitFor(() =>
      expect(screen.getByLabelText("保存的对话")).toHaveValue("created"),
    );
    expect(
      server.requests.find((r) => r.path === "/api/conversations")!.body,
    ).toEqual({
      agent_id: "ling",
      title: "新交流",
      ...(kind === "scene"
        ? {
            scene: "海岛相遇",
            background: "我们在虚构的海岛上探索。\n你是同行的伙伴。",
          }
        : {}),
    });
    if (kind === "scene") {
      await user.click(screen.getByText("查看场景背景"));
      expect(screen.getByText(/我们在虚构的海岛上探索/)).toBeVisible();
      expect(screen.queryByText(/背景描述不会注入/)).not.toBeInTheDocument();
    }
    await chooseModel(user, "备用模型");
    await user.click(
      screen.getByRole("button", { name: "对话模型与思考强度" }),
    );
    expect(screen.queryByText("思考强度")).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.type(screen.getByLabelText("消息"), "只有当前输入");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(
      server.requests.find((r) => r.path === "/api/chat/stream")!.body,
    ).toEqual({
      agent_id: "ling",
      conversation_id: "created",
      profile_id: "backup",
      request_id: expect.any(String),
      message: "只有当前输入",
    });
    expect(Object.values(sessionStorage).join(" ")).not.toMatch(
      /owner-fixture|example.invalid/,
    );
  },
);

test("history load failure offers retry, disables writes and cannot fall back to a browser transcript", async () => {
  const server = backend({ failMessages: true });
  const user = userEvent.setup();
  open();
  expect(await screen.findByRole("alert")).toBeVisible();
  expect(screen.queryByText("原回复")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  server.recover();
  await user.click(screen.getByRole("button", { name: "重新加载" }));
  expect(await screen.findByText("原回复")).toBeVisible();
});

test("late messages from the previously selected conversation cannot appear in the next conversation", async () => {
  const server = backend({ delayMessages: true });
  const user = userEvent.setup();
  open();
  await waitFor(() =>
    expect(screen.getByLabelText("保存的对话")).toHaveValue("c1"),
  );
  await user.selectOptions(screen.getByLabelText("保存的对话"), "c2");
  expect(await screen.findByText("岛上的问题")).toBeVisible();
  await act(async () => {
    server.releaseMessages!();
  });
  expect(screen.queryByText("原回复")).not.toBeInTheDocument();
});

test("failed regeneration retries the same existing user with a new request UUID", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  await user.click(screen.getByRole("button", { name: "重新生成" }));
  await server.emit("started", { conversation_id: "c1" }, 1);
  await server.emit(
    "error",
    { code: "provider_error", message: "fixture-private" },
    2,
  );
  const retry = await screen.findByRole("button", { name: "再次发送" });
  await waitFor(() => expect(retry).toBeEnabled());
  await user.click(retry);
  const requests = server.requests.filter(
    (request) => request.path === "/api/chat/stream",
  );
  expect(requests).toHaveLength(2);
  expect(requests[1].body).toMatchObject({
    conversation_id: "c1",
    regenerate_message_id: "u1",
    message: "原问题",
  });
  expect(requests[1].body?.request_id).not.toBe(requests[0].body?.request_id);
  expect(
    within(screen.getByRole("log")).getAllByRole("article", {
      name: "你的消息",
    }),
  ).toHaveLength(1);
});

test("a pending run reload joins server history to its user message ID and reconnects without another POST", async () => {
  const server = backend();
  const user = userEvent.setup();
  const view = open();
  await screen.findByText("原回复");
  await user.type(screen.getByLabelText("消息"), "正在运行的消息");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  await server.emit("started", { conversation_id: "c1" }, 1);
  await server.emit("text", { text: "部分生成" }, 2);
  view.unmount();
  open();
  expect(await screen.findByText("部分生成")).toBeVisible();
  await waitFor(() =>
    expect(
      server.requests.some(
        (request) => request.path === "/api/chat/runs/run/events?after=2",
      ),
    ).toBe(true),
  );
  expect(
    within(screen.getByRole("log")).getAllByText("正在运行的消息"),
  ).toHaveLength(1);
  expect(
    server.requests.filter((request) => request.path === "/api/chat/stream"),
  ).toHaveLength(1);
});

test("a terminal failure joins its persisted user node without displaying the optimistic message twice", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  await user.type(screen.getByLabelText("消息"), "失败的请求");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  await server.emit("started", { conversation_id: "c1" }, 1);
  await server.emit("text", { text: "失败前的部分正文" }, 2);
  await server.emit(
    "error",
    { code: "provider_error", message: "fixture-private" },
    3,
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "再次发送" })).toBeEnabled(),
  );
  expect(
    within(screen.getByRole("log")).getAllByText("失败的请求"),
  ).toHaveLength(1);
  expect(screen.getByText("失败前的部分正文")).toBeVisible();
  const request = server.requests.find(
    (request) => request.path === "/api/chat/stream",
  )!.body!;
  expect(
    within(screen.getByRole("log"))
      .getAllByRole("article", { name: "你的消息" })
      .at(-1),
  ).toHaveAttribute("data-message-id", "user-" + request.request_id);
});
