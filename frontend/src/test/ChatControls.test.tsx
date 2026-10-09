import { expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatControls } from "../features/chat/ChatControls";
import type { SavedModel } from "../api/types";

function model(id: string, name: string): SavedModel {
  return {
    id,
    name,
    credentials_available: true,
    profile: {
      api_family: "openai_chat_completions",
      model: "chat",
      base_url: "https://example.invalid/v1",
      capabilities: {
        streaming: true,
        tools: true,
        thinking: true,
        effort_levels: [],
      },
    },
  };
}

function open(overrides: Record<string, unknown> = {}) {
  const handlers = {
    onProfile: vi.fn(),
    onEffort: vi.fn(),
    onRefresh: vi.fn(),
  };
  render(
    <ChatControls
      profiles={[model("a", "主用连接"), model("b", "备用连接")]}
      profileId="a"
      effortLevels={["low", "high"]}
      effort="high"
      disabled={false}
      locked={false}
      loading={false}
      {...handlers}
      {...overrides}
    />,
  );
  return handlers;
}

test("胶囊上显示当前模型与思考强度", () => {
  open();
  const trigger = screen.getByRole("button", { name: "对话模型与思考强度" });
  expect(trigger).toHaveTextContent("主用连接");
  expect(trigger).toHaveTextContent("high");
});

test("选另一个模型会回调，并收起弹层", async () => {
  const { onProfile } = open();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  expect(screen.getByRole("radio", { name: "主用连接" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await user.click(screen.getByRole("radio", { name: "备用连接" }));
  expect(onProfile).toHaveBeenCalledWith("b");
  expect(screen.queryByRole("radio", { name: "备用连接" })).toBeNull();
});

test("选思考强度会回调，弹层保持打开以便连续调整", async () => {
  const { onEffort } = open();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  await user.click(screen.getByRole("radio", { name: "low" }));
  expect(onEffort).toHaveBeenCalledWith("low");
  expect(screen.getByRole("radio", { name: "high" })).toBeInTheDocument();
});

test("弹层里的按钮可以刷新模型列表", async () => {
  const { onRefresh } = open();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  await user.click(screen.getByRole("button", { name: "刷新模型列表" }));
  expect(onRefresh).toHaveBeenCalled();
});

test("按 Esc 收起弹层", async () => {
  open();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  await user.keyboard("{Escape}");
  expect(screen.queryByText("选择模型")).toBeNull();
});

test("模型不支持思考强度时不显示档位，胶囊上也不显示", async () => {
  open({ effortLevels: [], effort: "" });
  const trigger = screen.getByRole("button", { name: "对话模型与思考强度" });
  expect(trigger).toHaveTextContent("主用连接");
  const user = userEvent.setup();
  await user.click(trigger);
  expect(screen.getByText("选择模型")).toBeInTheDocument();
  expect(screen.queryByText("思考强度")).toBeNull();
});

test("没有可用连接时胶囊不可展开", () => {
  open({ profiles: [], profileId: "", disabled: true });
  expect(
    screen.getByRole("button", { name: "对话模型与思考强度" }),
  ).toBeDisabled();
  expect(screen.queryByRole("button", { name: "刷新模型列表" })).toBeNull();
});
