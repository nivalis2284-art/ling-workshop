import { expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../api/client";
import { ModelSettings } from "../features/settings/ModelSettings";

test("模型密钥仅提交到本地服务，保存后清空，选择模型不触发推理调用", async () => {
  const requests: { path: string; method: string; body: unknown }[] = [];
  let profiles: unknown[] = [];
  let selection = {
    primary: null as string | null,
    fallback: null as string | null,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      requests.push({ path: url, method, body });
      const json = (value: unknown) =>
        new Response(JSON.stringify(value), { status: 200 });
      if (url === "/api/models" && method === "GET") return json(profiles);
      if (url === "/api/models/selection" && method === "GET")
        return json(selection);
      if (url === "/api/models" && method === "POST") {
        profiles = [
          {
            id: "profile-1",
            name: body.name,
            profile: body.profile,
            credentials_available: true,
          },
        ];
        return json(profiles[0]);
      }
      if (url === "/api/models/selection" && method === "PUT") {
        selection = body;
        return json(selection);
      }
      throw new Error("Unexpected endpoint: " + url);
    }),
  );
  const user = userEvent.setup();
  render(<ModelSettings api={new LingApi("test-owner-only", () => {})} />);
  await user.type(await screen.findByLabelText("连接名称"), "测试连接");
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  await user.selectOptions(
    screen.getByLabelText("接口类型"),
    "openai_chat_completions",
  );
  await user.type(screen.getByLabelText("模型名称"), "test-model");
  await user.type(
    screen.getByLabelText("服务地址"),
    "https://models.example.invalid/v1",
  );
  const keyInput = screen.getByLabelText("模型密钥");
  await user.type(keyInput, "test-only-model-credential");
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  await waitFor(() =>
    expect(screen.getByLabelText("模型密钥")).toHaveValue(""),
  );
  const save = requests.find((request) => request.method === "POST");
  expect(save?.body).toMatchObject({
    name: "测试连接",
    api_key: "test-only-model-credential",
    profile: {
      api_family: "openai_chat_completions",
      model: "test-model",
      base_url: "https://models.example.invalid/v1",
      capabilities: {
        streaming: true,
        tools: true,
        thinking: false,
        effort_levels: [],
      },
    },
  });
  expect(Object.values(sessionStorage).join(" ")).not.toContain(
    "test-only-model-credential",
  );
  expect(Object.values(localStorage).join(" ")).not.toContain(
    "test-only-model-credential",
  );
  expect(document.body.textContent).not.toContain("test-only-model-credential");
  expect(screen.getByLabelText("主用模型")).toHaveValue("");
  await user.selectOptions(screen.getByLabelText("主用模型"), "profile-1");
  await user.click(screen.getByRole("button", { name: "保存模型选择" }));
  await waitFor(() =>
    expect(requests.find((request) => request.method === "PUT")?.body).toEqual({
      primary: "profile-1",
      fallback: null,
    }),
  );
  expect(
    requests.every((request) => request.path.startsWith("/api/models")),
  ).toBe(true);
});
