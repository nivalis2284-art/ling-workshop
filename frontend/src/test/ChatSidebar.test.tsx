import { expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
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

function node(
  id: string,
  role: "user" | "assistant",
  text: string,
  parent: string | null,
): ConversationMessage {
  return { id, role, text, parent_id: parent };
}

/** Two persisted rounds, so a deletion always leaves something to compare with. */
function backend() {
  const requests: {
    path: string;
    method: string;
    body: Record<string, unknown> | null;
  }[] = [];
  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), { status });
  let messages: ConversationMessage[] = [
    node("u1", "user", "第一个问题", null),
    node("a1", "assistant", "第一条回复", "u1"),
    node("u2", "user", "第二个问题", "a1"),
    node("a2", "assistant", "第二条回复", "u2"),
  ];
  const conversation = {
    id: "conversation-1",
    agent_id: "ling-1",
    title: "交流",
    scope: "real",
    head_id: "a2",
  };
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    const method = init.method ?? "GET";
    requests.push({
      path,
      method,
      body: init.body ? JSON.parse(String(init.body)) : null,
    });
    if (path === "/api/agents") return json([{ id: "ling-1", name: "灵" }]);
    if (method === "GET" && path.endsWith("/conversations"))
      return json([conversation]);
    if (path === "/api/conversations/conversation-1/messages")
      return json(messages);
    if (path === "/api/conversations/conversation-1/branches")
      return json(messages.length ? [{ ...messages.at(-1), selected: true }] : []);
    if (path === "/api/conversations/conversation-1/delete-turns") {
      const anchors = (
        JSON.parse(String(init.body)) as { anchors: string[] }
      ).anchors;
      const removed = new Set<string>();
      let dropping = false;
      for (const item of messages) {
        if (item.role === "user") dropping = anchors.includes(item.id);
        if (dropping) removed.add(item.id);
      }
      messages = messages.filter((item) => !removed.has(item.id));
      return json(conversation);
    }
    if (path.endsWith("/attachments")) return json([]);
    if (path === "/api/models") return json([profile]);
    if (path === "/api/models/selection")
      return json({ primary: profile.id, fallback: null });
    throw new Error("Unexpected endpoint: " + path);
  });
  return { requests };
}

async function openChat() {
  render(<App initialToken="test-owner" />);
  await userEvent.click(await screen.findByRole("button", { name: "对话" }));
  await screen.findByText("第一条回复");
}

test("the sidebar no longer lists chats and offers no way to create one", async () => {
  backend();
  await openChat();
  expect(
    screen.queryByRole("button", { name: "新聊天" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "主要导航" })).toBeVisible();
  expect(
    within(screen.getByRole("navigation", { name: "主要导航" })).queryByText(
      "最近",
    ),
  ).not.toBeInTheDocument();
});

test("deleting one round asks first and then drops it with its reply", async () => {
  const server = backend();
  await openChat();
  const mine = within(screen.getByRole("log")).getAllByRole("article", {
    name: "你的消息",
  });
  expect(mine).toHaveLength(2);
  await userEvent.click(
    within(mine[0]).getByRole("button", { name: "删除这一轮" }),
  );
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/无法撤销/)).toBeVisible();
  await userEvent.click(
    within(dialog).getByRole("button", { name: "确认删除" }),
  );
  await waitFor(() =>
    expect(screen.queryByText("第一个问题")).not.toBeInTheDocument(),
  );
  expect(screen.queryByText("第一条回复")).not.toBeInTheDocument();
  expect(screen.getByText("第二个问题")).toBeVisible();
  const request = server.requests.find((value) =>
    value.path.endsWith("/delete-turns"),
  )!;
  expect(request.method).toBe("POST");
  expect(request.body).toEqual({ anchors: ["u1"] });
});

test("cancelling the confirmation keeps the round", async () => {
  const server = backend();
  await openChat();
  const mine = within(screen.getByRole("log")).getAllByRole("article", {
    name: "你的消息",
  });
  await userEvent.click(within(mine[0]).getByRole("button", { name: "删除这一轮" }));
  await userEvent.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: "取消",
    }),
  );
  expect(screen.getByText("第一个问题")).toBeVisible();
  expect(
    server.requests.some((value) => value.path.endsWith("/delete-turns")),
  ).toBe(false);
});

test("batch deletion removes several rounds in one request", async () => {
  const server = backend();
  await openChat();
  await userEvent.click(screen.getByRole("button", { name: "对话选项" }));
  await userEvent.click(screen.getByRole("button", { name: "批量删除" }));
  const boxes = await screen.findAllByRole("checkbox");
  expect(boxes).toHaveLength(2);
  expect(screen.getByRole("button", { name: "删除所选" })).toBeDisabled();
  await userEvent.click(boxes[0]);
  await userEvent.click(boxes[1]);
  expect(screen.getByText("已选 2 轮")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "删除所选" }));
  await userEvent.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: "确认删除",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByText("第二个问题")).not.toBeInTheDocument(),
  );
  expect(screen.getByText(/从一条消息开始/)).toBeVisible();
  expect(
    server.requests.filter((value) => value.path.endsWith("/delete-turns")),
  ).toHaveLength(1);
});
