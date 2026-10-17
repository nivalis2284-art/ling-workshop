import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { LingApi } from "../../api/client";
import { ChatPage } from "../chat/ChatPage";
import { ChatSessions } from "../chat/session";
import type { ConversationList } from "../chat/useConversations";
import { ExperienceViewer } from "./ExperienceViewer";
import type {
  Agent,
  Conversation,
  MainConversation,
  WorkBranch,
} from "../../api/types";

const token = "continuity-owner-session";
const agent: Agent = { id: "ling", name: "灵" };
const mainConversation: Conversation = {
  id: "main-1",
  agent_id: "ling",
  title: "我们的日常",
  scope: "real",
  head_id: null,
};
const otherConversation: Conversation = {
  id: "old-1",
  agent_id: "ling",
  title: "旧会话",
  scope: "real",
  head_id: null,
};

type Request = { path: string; method: string; body: Record<string, unknown> | null };

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function chatBackend(options: {
  main?: MainConversation;
  work?: WorkBranch[];
  models?: boolean;
} = {}) {
  const requests: Request[] = [];
  let main: MainConversation = options.main ?? {
    conversation: null,
    revision: 0,
    choices: [mainConversation, otherConversation],
  };
  const work = [...(options.work ?? [])];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input, "http://localhost");
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : null;
      requests.push({ path: url.pathname, method, body });
      if (url.pathname === "/api/agents/ling/main-conversation" && method === "GET")
        return json(main);
      if (url.pathname === "/api/agents/ling/main-conversation" && method === "PUT") {
        const selected = [mainConversation, otherConversation].find((item) => item.id === body.conversation_id) ?? null;
        main = { conversation: selected, revision: main.revision + 1, choices: main.choices };
        return json(main);
      }
      if (url.pathname === "/api/agents/ling/work-branches" && method === "GET")
        return json(work);
      if (url.pathname === "/api/agents/ling/work-branches" && method === "POST") {
        const value: WorkBranch = {
          id: "work-1",
          agent_id: "ling",
          conversation_id: "branch-1",
          main_conversation_id: "main-1",
          origin_message_id: null,
          objective: body.objective,
          status: "running",
          revision: 1,
          created_at: "2026-10-17T04:00:00Z",
          started_at: "2026-10-17T04:00:01Z",
        };
        work.unshift(value);
        return json(value, 201);
      }
      const match = url.pathname.match(/^\/api\/work-branches\/([^/]+)\/(guide|cancel|finish)$/);
      if (match && method === "POST") {
        const index = work.findIndex((item) => item.id === match[1]);
        const current = work[index];
        const value: WorkBranch = {
          ...current,
          revision: current.revision + 1,
          status: match[2] === "cancel" ? "cancelled" : match[2] === "finish" ? "completed" : current.status,
        };
        work[index] = value;
        return json(value);
      }
      if (url.pathname === "/api/agents/ling/conversations") return json([mainConversation, otherConversation]);
      if (url.pathname === "/api/agents/ling/attachments") return json([]);
      if (url.pathname === "/api/models") return json(options.models === false ? [] : [
        {
          id: "profile-1",
          name: "本地测试",
          credentials_available: true,
          profile: {
            api_family: "openai_chat_completions",
            model: "mock",
            base_url: "http://localhost",
            capabilities: { streaming: true, tools: true, thinking: false, effort_levels: [] },
          },
        },
      ]);
      if (url.pathname === "/api/models/selection") return json({ primary: "profile-1", fallback: null });
      if (url.pathname === "/api/agents/ling/permissions") return json({ agent_id: "ling", revision: 0, readable: [], writable: [] });
      if (/^\/api\/conversations\/[^/]+\/works/.test(url.pathname) || /work-events/.test(url.pathname))
        return json([]);
      if (/^\/api\/conversations\/[^/]+\/(messages|branches|access)$/.test(url.pathname))
        return json(url.pathname.endsWith("access") ? { conversation_id: "main-1", mode: "confirm" } : []);
      throw new Error("Unexpected request " + method + " " + url.pathname);
    }),
  );
  return { requests, get main() { return main; }, work };
}

function conversationList(): ConversationList {
  return {
    list: [mainConversation, otherConversation],
    selectedId: undefined,
    choose: vi.fn(),
    adopt: vi.fn(),
    creating: false,
    setCreating: vi.fn(),
    loading: false,
    error: "",
    refresh: vi.fn(async () => {}),
  };
}

async function renderChat(options: Parameters<typeof chatBackend>[0] = {}) {
  const server = chatBackend(options);
  const api = new LingApi(token, () => {});
  const sessions = new ChatSessions(api);
  const user = userEvent.setup();
  render(
    <ChatPage
      api={api}
      agent={agent}
      session={sessions.forAgent(agent.id)}
      conversations={conversationList()}
      openSettings={() => {}}
      openBrowser={() => {}}
    />,
  );
  return { server, user };
}

test("first use requires an explicit trunk and keeps old conversations unmerged", async () => {
  const { server, user } = await renderChat();
  const setup = await screen.findByRole("region", { name: "选择主干" });
  expect(within(setup).getByText("我们的日常")).toBeVisible();
  expect(within(setup).getByText("旧会话")).toBeVisible();
  expect(server.requests.filter((request) => request.method === "PUT")).toHaveLength(0);
  await user.click(within(setup).getByRole("radio", { name: /我们的日常/ }));
  await user.click(within(setup).getByRole("button", { name: "使用所选会话作为主干" }));
  expect(await screen.findByText("主干 · 我们的日常")).toBeVisible();
  const bind = server.requests.find((request) => request.method === "PUT");
  expect(bind?.body).toEqual({ conversation_id: "main-1", expected_revision: 0 });
  expect(server.main.choices).toHaveLength(2);
});

test("work drawer starts an isolated task, guides it, finishes it, and keeps the composer present", async () => {
  const { server, user } = await renderChat({
    main: { conversation: mainConversation, revision: 2, choices: [mainConversation, otherConversation] },
  });
  await user.click(await screen.findByRole("button", { name: "工作与经历" }));
  const drawer = await screen.findByRole("complementary", { name: "工作与经历抽屉" });
  await user.type(within(drawer).getByRole("textbox", { name: "这次要完成什么" }), "修复登录空白");
  await user.click(within(drawer).getByRole("button", { name: "开始工作" }));
  const detail = await within(drawer).findByRole("region", { name: "工作分支详情" });
  expect(within(detail).getByText("修复登录空白")).toBeVisible();
  const start = server.requests.find((request) => request.path === "/api/agents/ling/work-branches" && request.method === "POST");
  expect(start?.body).toMatchObject({ objective: "修复登录空白", request_id: expect.any(String) });
  await user.type(within(detail).getByRole("textbox", { name: "给正在工作的一支补充指导" }), "先跑现有测试");
  await user.click(within(detail).getByRole("button", { name: "发送指导" }));
  await waitFor(() =>
    expect(server.requests.some((request) => request.path.endsWith("/guide"))).toBe(true),
  );
  await user.click(within(detail).getByRole("button", { name: "完成并归档" }));
  await waitFor(() => expect(server.requests.some((request) => request.path.endsWith("/finish"))).toBe(true));
  expect(screen.getByRole("textbox", { name: "消息" })).toBeVisible();
  expect(server.requests.some((request) => request.path === "/api/chat/stream")).toBe(false);
});

test("archived experience reads records by server cursor without any model or chat request", async () => {
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input, "http://localhost");
      requests.push({ path: url.pathname + url.search, method: init.method ?? "GET", body: null });
      if (url.pathname === "/api/agents/ling/experiences")
        return json({
          items: [{
            id: "exp-1",
            agent_id: "ling",
            kind: "work_branch",
            title: "登录修复",
            summary: "修复了空响应并验证测试。",
            revision: 1,
            occurred_start: "2026-10-16T02:00:00Z",
            occurred_end: "2026-10-16T03:00:00Z",
            coverage: "retained",
            valid: true,
            conversation_id: "branch-1",
            branch_id: "work-1",
            source_count: 1,
          }],
          next_cursor: null,
        });
      if (url.pathname === "/api/experiences/exp-1/records")
        return json({ items: [{ id: "r1", source_id: "source-1", kind: "message", location: "messages/1", coverage: "retained" }], next_cursor: null, revision: 1 });
      if (url.pathname === "/api/experiences/exp-1/records/source-1")
        return json({ text: "原文证据", next_cursor: null, sha256: "a".repeat(64), page_bytes: 12, revision: 1, coverage: "retained" });
      throw new Error("Unexpected request " + url.pathname);
    }),
  );
  const user = userEvent.setup();
  render(<ExperienceViewer api={new LingApi(token, () => {})} agentId="ling" />);
  await user.click(await screen.findByRole("button", { name: /登录修复/ }));
  await user.click(await screen.findByRole("button", { name: /messages\/1/ }));
  expect(await screen.findByText("原文证据")).toBeVisible();
  expect(requests.every((request) => request.path.startsWith("/api/"))).toBe(true);
  expect(requests.some((request) => request.path.includes("chat") || request.path.includes("models"))).toBe(false);
  expect(requests.find((request) => request.path.includes("/records/source-1"))?.path).toContain("max_bytes=8192");
});

test("a late main read after switching individuals cannot overwrite the current trunk", async () => {
  let resolveOld: ((value: Response) => void) | undefined;
  const oldStarted = new Promise<void>((resolve) => {
    vi.stubGlobal("fetch", vi.fn((input: string) => {
      const path = new URL(input, "http://localhost").pathname;
      if (path === "/api/agents/old/main-conversation")
        return new Promise<Response>((accept) => { resolveOld = accept; resolve(); });
      if (path === "/api/agents/new/main-conversation")
        return Promise.resolve(json({ conversation: { ...mainConversation, id: "new-main", title: "新灵的主干" }, revision: 1, choices: [] }));
      return Promise.resolve(json([]));
    }));
  });
  const sharedApi = new LingApi(token, () => {});
  const { rerender } = render(
    <ChatPage
      api={sharedApi}
      agent={{ id: "old", name: "旧灵" }}
      session={new ChatSessions(new LingApi(token, () => {})).forAgent("old")}
      conversations={{ ...conversationList(), list: [] }}
      openSettings={() => {}}
      openBrowser={() => {}}
    />,
  );
  await oldStarted;
  rerender(
    <ChatPage
      api={sharedApi}
      agent={{ id: "new", name: "新灵" }}
      session={new ChatSessions(new LingApi(token, () => {})).forAgent("new")}
      conversations={{ ...conversationList(), list: [] }}
      openSettings={() => {}}
      openBrowser={() => {}}
    />,
  );
  expect(await screen.findByText(/新灵的主干/)).toBeVisible();
  await act(async () => {
    resolveOld?.(json({ conversation: { ...mainConversation, title: "迟到的旧主干" }, revision: 1, choices: [] }));
  });
  expect(screen.queryByText(/迟到的旧主干/)).not.toBeInTheDocument();
});
