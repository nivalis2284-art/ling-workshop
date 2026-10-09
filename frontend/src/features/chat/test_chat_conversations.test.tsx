import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ChatHarness } from "./testHarness";
import { ChatSessions } from "./session";
import { LingApi } from "../../api/client";
import type {
  ConversationMessage,
  Conversation,
  SavedModel,
} from "../../api/types";

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
      /^\/api\/conversations\/([^/]+)\/(messages|branches|edit|head|delete-turns)$/,
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
      if (match[2] === "delete-turns") {
        const anchors: string[] = body.anchors ?? [];
        const snapshot: ConversationMessage[] = paths[value.head_id ?? ""] ?? [];
        const removed = new Set<string>();
        let dropping = false;
        for (const item of snapshot) {
          if (item.role === "user") dropping = anchors.includes(item.id);
          if (dropping) removed.add(item.id);
        }
        const survivors = snapshot.filter((item) => !removed.has(item.id));
        for (const key of Object.keys(paths))
          paths[key] = paths[key].filter((item) => !removed.has(item.id));
        paths[survivors.at(-1)?.id ?? ""] = survivors;
        value.head_id = survivors.at(-1)?.id ?? null;
        return json(value);
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
    ...render(<ChatHarness api={api} />),
    sessions,
  };
}

/** Branch switching moved behind the conversation's ⋯ menu. */
async function openChatMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "对话选项" }));
}

test("backend transcripts survive fresh browser storage and sessions stay per conversation", async () => {
  const server = backend();
  const view = open();
  expect(await screen.findByText("原回复")).toBeVisible();
  expect(screen.queryByText(/当前浏览器会话/)).not.toBeInTheDocument();
  expect(view.sessions.forAgent("ling", "c1")).not.toBe(
    view.sessions.forAgent("ling", "c2"),
  );
  view.unmount();
  sessionStorage.clear();
  open();
  expect(await screen.findByText("原回复")).toBeVisible();
  expect(
    server.requests.filter((r) => r.path.endsWith("/c1/messages")).length,
  ).toBeGreaterThanOrEqual(2);
});

test("editing a user forks the transcript and assistants cannot be edited", async () => {
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
  // Branch switching is no longer a UI control, so what the owner sees is the
  // fork itself: a new node for the edited text and no stale reply behind it.
  expect(screen.getByRole("article", { name: "你的消息" })).toHaveAttribute(
    "data-message-id",
    "edited",
  );
  expect(
    within(log).queryByRole("article", { name: "灵的回复" }),
  ).not.toBeInTheDocument();
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
  // The composer stays usable while a reply is being generated.
  expect(screen.getByLabelText("消息")).toBeEnabled();
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
  // The user line is reused rather than duplicated; branch switching is no
  // longer a UI control, so the source id is the observable proof.
  expect(screen.getByRole("article", { name: "你的消息" })).toHaveAttribute(
    "data-message-id",
    "u1",
  );
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


test("late messages from another conversation cannot appear in the current one", async () => {
  const server = backend({ delayMessages: true });
  // The first conversation's messages are held back on purpose.
  const first = open();
  first.unmount();
  // The remembered conversation is how a second one is reached without the list.
  sessionStorage.setItem("lingnexus.chat.v1.choice.ling", "c2");
  open();
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

/** Mirrors the established send pattern so a second round exists to compare with. */
async function secondRound(
  user: ReturnType<typeof userEvent.setup>,
  server: ReturnType<typeof backend>,
) {
  await user.type(screen.getByLabelText("消息"), "第二个问题");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  await server.emit("started", { conversation_id: "c1" }, 1);
  await server.emit("text", { text: "第二条回复" }, 2);
  await server.emit("complete", { status: "completed", assistant_source: null }, 3);
  await screen.findByText("第二个问题");
}

test("deleting a round asks first, then removes it with its reply", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  await secondRound(user, server);
  const log = screen.getByRole("log");
  // Only the owner's messages offer deletion; a reply is removed with its round.
  expect(
    within(screen.getByRole("article", { name: "灵的回复" })).queryByRole(
      "button",
      { name: "删除这一轮" },
    ),
  ).not.toBeInTheDocument();
  const mine = within(log).getAllByRole("article", { name: "你的消息" });
  expect(mine).toHaveLength(2);
  await user.click(within(mine[0]).getByRole("button", { name: "删除这一轮" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/无法撤销/)).toBeInTheDocument();
  expect(screen.queryByText("原问题")).toBeVisible();
  await user.click(within(dialog).getByRole("button", { name: "确认删除" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await waitFor(() => expect(screen.queryByText("原问题")).not.toBeInTheDocument());
  expect(screen.queryByText("原回复")).not.toBeInTheDocument();
  expect(screen.getByText("第二个问题")).toBeVisible();
  expect(server.requests.find((r) => r.path.endsWith("/delete-turns"))?.body)
    .toEqual({ anchors: ["u1"] });
});

test("a cancelled confirmation keeps the round", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  await secondRound(user, server);
  await user.click(
    within(
      within(screen.getByRole("log")).getAllByRole("article", {
        name: "你的消息",
      })[0],
    ).getByRole("button", { name: "删除这一轮" }),
  );
  await user.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: "取消",
    }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByText("原问题")).toBeVisible();
  expect(
    server.requests.some((r) => r.path.endsWith("/delete-turns")),
  ).toBe(false);
});

test("batch deletion removes several rounds in one request", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  await secondRound(user, server);
  await openChatMenu(user);
  await user.click(screen.getByRole("button", { name: "批量删除" }));
  const boxes = await screen.findAllByRole("checkbox");
  expect(boxes).toHaveLength(2);
  expect(screen.getByRole("button", { name: "删除所选" })).toBeDisabled();
  await user.click(boxes[0]);
  await user.click(boxes[1]);
  expect(screen.getByText("已选 2 轮")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "删除所选" }));
  await user.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: "确认删除",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByText("第二个问题")).not.toBeInTheDocument(),
  );
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  const sent = server.requests.find((r) => r.path.endsWith("/delete-turns"))!
    .body as { anchors: string[] };
  expect(sent.anchors).toHaveLength(2);
  expect(sent.anchors[0]).toBe("u1");
});
