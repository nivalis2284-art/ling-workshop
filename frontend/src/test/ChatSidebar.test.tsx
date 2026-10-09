import { expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Shell } from "../app/Shell";
import { ChatHistory } from "../features/chat/ChatHistory";
import type { ConversationList } from "../features/chat/useConversations";
import type { Conversation } from "../api/types";

const agent = { id: "ling", name: "灵" };

function conversation(id: string, title: string, scene = false): Conversation {
  return {
    id,
    agent_id: "ling",
    title,
    scope: scene ? "scene:island" : "real",
    head_id: null,
  };
}

function list(overrides: Partial<ConversationList> = {}): ConversationList {
  return {
    list: [conversation("a", "日常记录"), conversation("b", "海岛故事", true)],
    selectedId: "a",
    choose: vi.fn(),
    adopt: vi.fn(),
    creating: false,
    setCreating: vi.fn(),
    loading: false,
    error: "",
    refresh: vi.fn(),
    ...overrides,
  };
}

test("对话列表列在侧栏里，当前那条被标出来", () => {
  render(
    <ChatHistory
      conversations={list()}
      currentId="a"
      disabled={false}
      onSelect={() => {}}
      onNew={() => {}}
    />,
  );
  expect(screen.getByRole("button", { current: true })).toHaveTextContent(
    "日常记录",
  );
  expect(screen.getByRole("button", { name: /^海岛故事/ })).toHaveTextContent(
    "场景",
  );
});

test("点另一条对话会切换，「新聊天」会请求新建", async () => {
  const onSelect = vi.fn();
  const onNew = vi.fn();
  render(
    <ChatHistory
      conversations={list()}
      currentId="a"
      disabled={false}
      onSelect={onSelect}
      onNew={onNew}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^海岛故事/ }));
  expect(onSelect).toHaveBeenCalledWith("b");
  await user.click(screen.getByRole("button", { name: "新聊天" }));
  expect(onNew).toHaveBeenCalled();
});

test("没有对话时给出可读的空状态而不是一片空白", () => {
  render(
    <ChatHistory
      conversations={list({ list: [], selectedId: undefined })}
      currentId=""
      disabled={false}
      onSelect={() => {}}
      onNew={() => {}}
    />,
  );
  expect(screen.getByText(/还没有对话/)).toBeInTheDocument();
});

test("读取失败时提供重新加载", async () => {
  const refresh = vi.fn();
  render(
    <ChatHistory
      conversations={list({ list: [], error: "读取失败", refresh })}
      currentId=""
      disabled={false}
      onSelect={() => {}}
      onNew={() => {}}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(refresh).toHaveBeenCalled();
});

test("侧栏可以收起并记住状态", async () => {
  localStorage.clear();
  render(
    <Shell
      agents={[agent]}
      agent={agent}
      page="chat"
      onNavigate={() => {}}
      onSelectAgent={() => {}}
    >
      <p>内容</p>
    </Shell>,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "收起侧栏" }));
  expect(document.querySelector(".app-shell")).toHaveClass("collapsed");
  expect(screen.getByRole("button", { name: "展开侧栏" })).toBeInTheDocument();
  expect(localStorage.getItem("lingnexus.shell.collapsed")).toBe("1");
  await user.click(screen.getByRole("button", { name: "展开侧栏" }));
  expect(document.querySelector(".app-shell")).not.toHaveClass("collapsed");
  expect(localStorage.getItem("lingnexus.shell.collapsed")).toBe("0");
  localStorage.clear();
});
