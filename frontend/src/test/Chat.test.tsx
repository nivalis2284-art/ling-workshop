import { expect, test, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../app/App";
import type { SavedModel, ConversationMessage } from "../api/types";

const profile: SavedModel = {
  id: "primary-profile",
  name: "主用连接",
  credentials_available: true,
  profile: {
    api_family: "openai_chat_completions",
    model: "chat-model",
    base_url: "https://example.invalid/v1",
    capabilities: {
      streaming: true,
      tools: true,
      thinking: true,
      effort_levels: [],
    },
  },
};

function backend(
  options: {
    profiles?: SavedModel[];
    rejectStart?: boolean;
    cancelFails?: boolean;
    cancelStatus?: string;
    startError?: { status: number; detail: string };
  } = {},
) {
  const requests: {
    path: string;
    body: Record<string, unknown> | null;
    signal?: AbortSignal | null;
  }[] = [];
  const streams: ReadableStreamDefaultController<Uint8Array>[] = [];
  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), { status });
  let rejectStart = options.rejectStart;
  let runId = "";
  let status = "running";
  let conversationCreated = false;
  let requestId = "";
  let answer = "";
  let assistantSource = { id: "assistant-source", revision: 1 };
  let userMessageId = "";
  const messages: ConversationMessage[] = [];
  const submitted = new Set<string>();
  const received = new Set<number>();
  const conversation = () => ({
    id: "conversation-1",
    agent_id: "ling-1",
    title: "日常交流",
    scope: "real",
    head_id: messages.at(-1)?.id ?? null,
  });
  const commit = () => {
    if (!messages.some((message) => message.source.id === assistantSource.id))
      messages.push({
        id: "answer-" + requestId,
        role: "assistant",
        text: answer,
        parent_id: userMessageId,
        source: assistantSource,
      });
  };
  const run = () => ({
    id: runId,
    agent_id: "ling-1",
    request_id: requestId,
    conversation_id: "conversation-1",
    user_message_id: userMessageId,
    profile_id: profile.id,
    status,
    user_source: { id: "user-source", revision: 1 },
    assistant_source: status === "completed" ? assistantSource : null,
    context_sources: [{ id: "user-source", revision: 1 }],
    created_at: "2026-10-09T00:00:00Z",
    finished_at: status === "running" ? null : "2026-10-09T00:01:00Z",
  });
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer test-owner",
    );
    requests.push({
      path,
      body: init.body ? JSON.parse(String(init.body)) : null,
      signal: init.signal,
    });
    if (path === "/api/agents")
      return json([
        { id: "ling-1", name: "小灵" },
        { id: "ling-2", name: "另一个灵" },
      ]);
    if (path.endsWith("/conversations"))
      return json(conversationCreated ? [conversation()] : []);
    if (path.endsWith("/attachments")) return json([]);
    if (path === "/api/conversations/conversation-1/messages")
      return json(messages);
    if (path === "/api/conversations/conversation-1/branches")
      return json(
        messages.length ? [{ ...messages.at(-1), selected: true }] : [],
      );
    if (path.endsWith("/state"))
      return json({ agent_id: "ling-1", revision: 0, entries: {} });
    if (
      path.endsWith("/proposals") ||
      path.endsWith("/revisions") ||
      path.includes("/episodes?")
    )
      return json([]);
    if (path === "/api/models")
      return json(
        options.profiles ?? [
          { ...profile, id: "other-profile", name: "另一连接" },
          profile,
        ],
      );
    if (path === "/api/models/selection")
      return json({ primary: "primary-profile", fallback: null });
    if (path.endsWith("/cancel")) {
      if (options.cancelFails) return json({ detail: "unavailable" }, 503);
      status = options.cancelStatus ?? "cancelled";
      return json(run());
    }
    if (path === "/api/chat/runs/" + runId) return json(run());
    if (path === "/api/chat/stream" || path.includes("/events?")) {
      if (path === "/api/chat/stream" && options.startError)
        return json(
          { detail: options.startError.detail },
          options.startError.status,
        );
      if (rejectStart) {
        rejectStart = false;
        throw new TypeError("connection lost");
      }
      if (path === "/api/chat/stream") {
        const body = JSON.parse(String(init.body));
        if (!submitted.has(body.request_id)) {
          submitted.add(body.request_id);
          requestId = body.request_id;
          received.clear();
          status = "running";
          answer = "";
          conversationCreated = true;
          userMessageId = "user-" + requestId;
          messages.push({
            id: userMessageId,
            role: "user",
            text: body.message,
            parent_id: messages.at(-1)?.id ?? null,
            source: { id: "user-source-" + requestId, revision: 1 },
          });
        }
      }
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            streams.push(controller);
          },
        }),
        {
          headers: { "Content-Type": "text/event-stream" },
        },
      );
    }
    throw new Error("Unexpected endpoint: " + path);
  });
  return {
    requests,
    setStatus(value: string) {
      status = value;
      if (status === "completed") commit();
    },
    async emit(
      type: string,
      data: Record<string, unknown>,
      seq: number,
      index = streams.length - 1,
    ) {
      if (typeof data.run_id === "string") runId = data.run_id;
      if (type === "text" && !received.has(seq)) answer += data.text;
      received.add(seq);
      if (type === "complete") {
        status = "completed";
        assistantSource = data.assistant_source as typeof assistantSource;
        commit();
      }
      if (type === "error")
        status =
          data.code === "cancelled"
            ? "cancelled"
            : data.code === "interrupted"
              ? "interrupted"
              : "failed";
      await act(async () => {
        streams[index].enqueue(
          new TextEncoder().encode(
            "id: " +
              seq +
              "\nevent: " +
              type +
              "\ndata: " +
              JSON.stringify({
                run_id: runId,
                ...(type === "started"
                  ? { conversation_id: "conversation-1" }
                  : {}),
                ...data,
              }) +
              "\n\n",
          ),
        );
      });
    },
    async disconnect() {
      await act(async () => {
        streams.at(-1)!.close();
      });
    },
  };
}

async function openChat() {
  const user = userEvent.setup();
  const view = render(<App initialToken="test-owner" />);
  await user.click(await screen.findByRole("button", { name: /^对话$/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("对话模型")).toHaveValue("primary-profile"),
  );
  return { user, ...view };
}

async function send(user: ReturnType<typeof userEvent.setup>, text = "你好") {
  await user.type(screen.getByRole("textbox", { name: "消息" }), text);
  await user.click(screen.getByRole("button", { name: "发送消息" }));
}

test("chat starts empty, selects the primary profile, and displays actual streaming text and collapsible thinking", async () => {
  const server = backend();
  const { user } = await openChat();
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "活动" }),
  ).not.toBeInTheDocument();
  await send(user);
  const start = server.requests.find(
    (request) => request.path === "/api/chat/stream",
  )!;
  expect(start.body).toEqual({
    agent_id: "ling-1",
    profile_id: "primary-profile",
    message: "你好",
    request_id: expect.any(String),
  });
  await server.emit("started", { run_id: "run-1" }, 1);
  await server.emit("thinking", { text: "思考摘要" }, 2);
  await server.emit("text", { text: "你好，" }, 3);
  expect(await screen.findByText("你好，")).toBeVisible();
  expect(screen.getByText("思考摘要")).not.toBeVisible();
  await user.click(screen.getByText("思考过程"));
  expect(screen.getByText("思考摘要")).toBeVisible();
  await server.emit("text", { text: "很高兴见到你。" }, 4);
  await server.emit(
    "usage",
    { usage: { input_tokens: 138, output_tokens: 12, total_tokens: 209 } },
    5,
  );
  await server.emit(
    "complete",
    {
      status: "completed",
      assistant_source: { id: "assistant-source", revision: 1 },
    },
    6,
  );
  expect(screen.getByText("你好，很高兴见到你。")).toBeVisible();
  const usage = screen.getByText(/输入 138.*输出 12.*服务商总计 209/);
  expect(usage).toBeVisible();
  expect(usage).toHaveAttribute(
    "title",
    expect.stringContaining("总计可能不等于输入与输出之和"),
  );
  expect(
    screen.queryByRole("button", { name: "停止生成" }),
  ).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("对话模型"), "other-profile");
  await send(user, "换个连接");
  expect(
    server.requests.filter((request) => request.path === "/api/chat/stream")[1]
      .body?.profile_id,
  ).toBe("other-profile");
});

test("Stop posts explicit cancellation before detaching and retains the partial answer", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit("started", { run_id: "run-stop" }, 1);
  await server.emit("text", { text: "部分回复" }, 2);
  const signal = server.requests.find(
    (request) => request.path === "/api/chat/stream",
  )!.signal!;
  expect(signal.aborted).toBe(false);
  await user.click(screen.getByRole("button", { name: "停止生成" }));
  await waitFor(() => expect(signal.aborted).toBe(true));
  expect(
    server.requests
      .filter((request) => request.path.endsWith("/cancel"))
      .map((request) => request.path),
  ).toEqual(["/api/chat/runs/run-stop/cancel"]);
  expect(screen.getByText("部分回复")).toBeVisible();
  expect(screen.getByText(/已停止/)).toBeVisible();
});

test("network loss reconnects the same run after the last event without duplicating text or POST", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit("started", { run_id: "run-reconnect" }, 1);
  await server.emit("text", { text: "第一部分" }, 2);
  await server.disconnect();
  await user.click(await screen.findByRole("button", { name: "重新连接" }));
  expect(server.requests.at(-1)?.path).toBe(
    "/api/chat/runs/run-reconnect/events?after=2",
  );
  await server.emit("text", { text: "第一部分" }, 2);
  await server.emit("text", { text: "第二部分" }, 3);
  await server.emit(
    "complete",
    {
      status: "completed",
      assistant_source: { id: "assistant-source", revision: 1 },
    },
    4,
  );
  expect(screen.getByText("第一部分第二部分")).toBeVisible();
  expect(
    server.requests.filter((request) => request.path === "/api/chat/stream"),
  ).toHaveLength(1);
});

test("unknown run retry reuses request_id after the POST connection fails", async () => {
  const server = backend({ rejectStart: true });
  const { user } = await openChat();
  await send(user);
  await user.click(await screen.findByRole("button", { name: "重新连接" }));
  const posts = server.requests.filter(
    (request) => request.path === "/api/chat/stream",
  );
  expect(posts).toHaveLength(2);
  expect(posts[1].body).toEqual(posts[0].body);
  expect(within(screen.getByRole("log")).getAllByText("你好")).toHaveLength(1);
});

test("unmount detaches without cancellation and remount restores run and cursor from this session", async () => {
  const server = backend();
  const { user, unmount } = await openChat();
  await send(user);
  await server.emit("started", { run_id: "run-retained" }, 1);
  await server.emit("text", { text: "保留下来的部分" }, 2);
  const signal = server.requests.find(
    (request) => request.path === "/api/chat/stream",
  )!.signal!;
  unmount();
  expect(signal.aborted).toBe(true);
  expect(
    server.requests.filter((request) => request.path.endsWith("/cancel")),
  ).toHaveLength(0);
  await openChat();
  await waitFor(() =>
    expect(
      server.requests.some(
        (request) =>
          request.path === "/api/chat/runs/run-retained/events?after=2",
      ),
    ).toBe(true),
  );
  expect(screen.getByText("保留下来的部分")).toBeVisible();
  expect(Object.values(sessionStorage).join(" ")).not.toContain("test-owner");
  await server.emit(
    "complete",
    {
      status: "completed",
      assistant_source: { id: "assistant-source", revision: 1 },
    },
    3,
  );
});

test.each(["missing_credentials", "provider_error"])(
  "backend %s is translated into actionable Chinese guidance",
  async (code) => {
    const server = backend();
    const { user } = await openChat();
    await send(user);
    await server.emit("started", { run_id: "run-error" }, 1);
    await server.emit("error", { code, message: "Safe provider error" }, 2);
    expect(await screen.findByRole("alert")).toHaveTextContent(/设置.*密钥/);
    expect(screen.getByRole("button", { name: "再次发送" })).toBeEnabled();
    expect(screen.queryByText(code)).not.toBeInTheDocument();
  },
);

test("failed explicit cancellation does not claim the task stopped or detach the subscriber", async () => {
  const server = backend({ cancelFails: true });
  const { user } = await openChat();
  await send(user);
  await server.emit("started", { run_id: "run-busy" }, 1);
  await user.click(screen.getByRole("button", { name: "停止生成" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/停止.*失败/);
  expect(
    server.requests.find((request) => request.path === "/api/chat/stream")!
      .signal!.aborted,
  ).toBe(false);
  expect(screen.queryByText(/已停止/)).not.toBeInTheDocument();
});

test("without model profiles the composer guides the user to settings and sends no request", async () => {
  const server = backend({ profiles: [] });
  const user = userEvent.setup();
  render(<App initialToken="test-owner" />);
  await user.click(await screen.findByRole("button", { name: /^对话$/ }));
  expect(await screen.findByText(/还没有模型连接/)).toBeVisible();
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(
    server.requests.some((request) => request.path === "/api/chat/stream"),
  ).toBe(false);
});

test("data received without started still establishes the run, survives reload and resumes after its sequence", async () => {
  const server = backend();
  const { user, unmount } = await openChat();
  await send(user);
  await server.emit(
    "text",
    { run_id: "run-data-first", text: "先收到的正文" },
    12,
  );
  expect(await screen.findByText("先收到的正文")).toBeVisible();
  expect(screen.getByRole("button", { name: "停止生成" })).toBeEnabled();
  unmount();
  await openChat();
  await waitFor(() =>
    expect(
      server.requests.some(
        (item) => item.path === "/api/chat/runs/run-data-first/events?after=12",
      ),
    ).toBe(true),
  );
  await server.emit("text", { text: "，接续正文" }, 13);
  await server.emit(
    "complete",
    {
      status: "completed",
      assistant_source: { id: "source-data-first", revision: 1 },
    },
    14,
  );
  expect(screen.getByText("先收到的正文，接续正文")).toBeVisible();
  expect(screen.getByText("回复完成")).toBeVisible();
  expect(
    server.requests.filter((item) => item.path === "/api/chat/stream"),
  ).toHaveLength(1);
});

test("an event from another run cannot contaminate this transcript or advance its cursor", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit("started", { run_id: "run-original" }, 1);
  await server.emit("text", { run_id: "run-wrong", text: "不属于这个对话" }, 2);
  expect(await screen.findByRole("button", { name: "重新连接" })).toBeEnabled();
  expect(screen.queryByText("不属于这个对话")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新连接" }));
  expect(server.requests.at(-1)?.path).toBe(
    "/api/chat/runs/run-original/events?after=1",
  );
});

test("cancelled error events are a stopped run, including cancellation from another subscriber", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit(
    "error",
    {
      run_id: "run-cancelled",
      code: "cancelled",
      message: "The owner cancelled this run.",
    },
    3,
  );
  expect(await screen.findByText(/已停止/)).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "停止生成" }),
  ).not.toBeInTheDocument();
});

test("a provider-side transport error is terminal and offers a new send, not reconnect guidance", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit(
    "error",
    {
      run_id: "run-failed",
      code: "transport_error",
      message: "The model connection failed.",
    },
    2,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /模型.*连接.*失败/,
  );
  expect(screen.getByRole("alert")).not.toHaveTextContent(
    /重新连接当前生成|仍在继续/,
  );
  expect(screen.getByRole("button", { name: "再次发送" })).toBeEnabled();
  expect(
    screen.queryByRole("button", { name: "重新连接" }),
  ).not.toBeInTheDocument();
});

test("an empty replay of a terminal run is resolved using GET run status", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit("text", { run_id: "run-terminal", text: "已有正文" }, 7);
  await server.disconnect();
  await user.click(await screen.findByRole("button", { name: "重新连接" }));
  server.setStatus("completed");
  await server.disconnect();
  expect(await screen.findByText("回复完成")).toBeVisible();
  expect(
    server.requests.some((item) => item.path === "/api/chat/runs/run-terminal"),
  ).toBe(true);
  expect(
    screen.queryByRole("button", { name: "重新连接" }),
  ).not.toBeInTheDocument();
});

test("Stop racing a completed run replays the missing final answer instead of claiming cancellation", async () => {
  const server = backend({ cancelStatus: "completed" });
  const { user } = await openChat();
  await send(user);
  await server.emit("text", { run_id: "run-race", text: "开头" }, 2);
  await user.click(screen.getByRole("button", { name: "停止生成" }));
  await waitFor(() =>
    expect(
      server.requests.some(
        (item) => item.path === "/api/chat/runs/run-race/events?after=2",
      ),
    ).toBe(true),
  );
  await server.emit("text", { text: "和结尾" }, 3);
  await server.emit(
    "complete",
    {
      status: "completed",
      assistant_source: { id: "assistant-source", revision: 1 },
    },
    4,
  );
  expect(screen.getByText("开头和结尾")).toBeVisible();
  expect(screen.queryByText(/已停止/)).not.toBeInTheDocument();
  expect(
    server.requests.filter((item) => item.path === "/api/chat/stream"),
  ).toHaveLength(1);
});

test("missing credentials rejected before started shows Chinese settings guidance", async () => {
  const server = backend({
    startError: { status: 403, detail: "本次运行尚未配置该模型的凭据" },
  });
  const { user } = await openChat();
  await send(user);
  expect(await screen.findByRole("alert")).toHaveTextContent(/设置.*密钥/);
  expect(
    screen.queryByRole("button", { name: "重新连接" }),
  ).not.toBeInTheDocument();
  expect(
    server.requests.filter((item) => item.path === "/api/chat/stream"),
  ).toHaveLength(1);
});

test("server interruption is terminal with Chinese recovery guidance and never displays provider details", async () => {
  const server = backend();
  const { user } = await openChat();
  await send(user);
  await server.emit(
    "text",
    { run_id: "run-interrupted", text: "中断前的正文" },
    2,
  );
  await server.emit(
    "error",
    { code: "interrupted", message: "fixture-provider-private-detail" },
    3,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(/本地服务.*中断/);
  expect(screen.getByText("中断前的正文")).toBeVisible();
  expect(screen.getByRole("button", { name: "再次发送" })).toBeEnabled();
  expect(document.body.textContent).not.toContain(
    "fixture-provider-private-detail",
  );
  expect(Object.values(sessionStorage).join(" ")).not.toContain(
    "fixture-provider-private-detail",
  );
});
