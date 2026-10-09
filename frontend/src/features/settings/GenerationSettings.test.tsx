import { expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LingApi } from "../../api/client";
import type { ModelInput, SavedModel } from "../../api/types";
import { ModelEditor } from "./ModelEditor";

function existing(): SavedModel {
  return {
    id: "original-profile",
    name: "日常预设",
    credentials_available: true,
    profile: {
      api_family: "anthropic_messages",
      model: "declared-model",
      base_url: "https://models.invalid/v1",
      capabilities: {
        streaming: true,
        tools: true,
        thinking: true,
        effort_levels: ["low", "high"],
      },
      context_window_tokens: 64000,
      max_output_tokens: 8192,
    },
  };
}

function setup(item = existing()) {
  const requests: { path: string; method: string; body: ModelInput }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as ModelInput;
      requests.push({ path, method: init?.method ?? "GET", body });
      if (path !== "/api/models" || init?.method !== "POST")
        throw new Error("Unexpected request");
      return new Response(
        JSON.stringify({
          id: body.profile_id ?? "new-profile",
          name: body.name,
          profile: body.profile,
          credentials_available: Boolean(body.api_key),
        }),
        { status: 200 },
      );
    }),
  );
  const saved = vi.fn(async () => {});
  render(
    <ModelEditor
      api={new LingApi("fixture-owner", () => {})}
      existing={item}
      saved={saved}
    />,
  );
  return { user: userEvent.setup(), requests, saved };
}

test("legacy profiles leave sampling and cache disabled and prices unknown", async () => {
  const { user, requests } = setup();
  await user.click(screen.getByText("高级选项：生成设置与费用"));
  expect(screen.getByLabelText("默认思考强度")).toHaveValue("");
  expect(screen.getByLabelText("温度（temperature）")).toBeDisabled();
  expect(screen.getByLabelText("核采样（top_p）")).toBeDisabled();
  expect(screen.getByLabelText("缓存稳定系统前缀")).not.toBeChecked();
  expect(screen.getByLabelText("缓存稳定系统前缀")).toBeDisabled();
  expect(screen.getByLabelText("输入价格（USD / 百万 token）")).toHaveValue(
    null,
  );
  expect(screen.getByLabelText("输出价格（USD / 百万 token）")).toHaveValue(
    null,
  );
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body.profile.temperature ?? null).toBeNull();
  expect(requests[0].body.profile.top_p ?? null).toBeNull();
  expect(requests[0].body.profile.input_price_per_million ?? null).toBeNull();
  expect(requests[0].body.profile.cache_system_prompt ?? false).toBe(false);
});

test("declared generation defaults and numeric prices save and reload", async () => {
  const { user, requests, saved } = setup();
  await user.click(screen.getByText("高级选项：服务支持的能力"));
  await user.click(screen.getByLabelText("支持温度参数"));
  await user.click(screen.getByLabelText("支持核采样参数"));
  await user.click(screen.getByLabelText("支持 Anthropic 系统前缀缓存"));
  await user.click(screen.getByText("高级选项：生成设置与费用"));
  await user.selectOptions(screen.getByLabelText("默认思考强度"), "low");
  await user.type(screen.getByLabelText("温度（temperature）"), "0.4");
  await user.type(screen.getByLabelText("核采样（top_p）"), "0.9");
  await user.click(screen.getByLabelText("缓存稳定系统前缀"));
  await user.type(screen.getByLabelText("输入价格（USD / 百万 token）"), "2.5");
  await user.type(screen.getByLabelText("输出价格（USD / 百万 token）"), "10");
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(requests[0].body).toMatchObject({
    profile_id: "original-profile",
    profile: {
      default_effort: "low",
      temperature: 0.4,
      top_p: 0.9,
      cache_system_prompt: true,
      input_price_per_million: 2.5,
      output_price_per_million: 10,
      capabilities: { temperature: true, top_p: true, prompt_caching: true },
    },
  });
  expect(requests.every((r) => r.path === "/api/models")).toBe(true);
});

test("removing a declared effort rejects an invalid default before saving", async () => {
  const item = existing();
  item.profile.default_effort = "high";
  const { user, requests } = setup(item);
  await user.click(screen.getByText("高级选项：服务支持的能力"));
  await user.clear(screen.getByLabelText("支持的思考强度"));
  await user.type(screen.getByLabelText("支持的思考强度"), "low");
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "默认思考强度必须在支持的档位中",
  );
  expect(requests).toHaveLength(0);
});

test("turning off capabilities clears optional values and changing family clears cache opt-in", async () => {
  const item = existing();
  Object.assign(item.profile.capabilities, {
    temperature: true,
    top_p: true,
    prompt_caching: true,
  });
  Object.assign(item.profile, {
    temperature: 0.4,
    top_p: 0.9,
    cache_system_prompt: true,
  });
  const { user, requests } = setup(item);
  await user.click(screen.getByText("高级选项：服务支持的能力"));
  await user.click(screen.getByLabelText("支持温度参数"));
  await user.click(screen.getByLabelText("支持核采样参数"));
  await user.selectOptions(
    screen.getByLabelText("接口类型"),
    "openai_responses",
  );
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body.profile).toMatchObject({
    temperature: null,
    top_p: null,
    cache_system_prompt: false,
    capabilities: { temperature: false, top_p: false, prompt_caching: false },
  });
});

test("clone is deliberate, preserves config, and requires a separately entered memory-only key", async () => {
  const item = existing();
  Object.assign(item.profile, {
    default_effort: "low",
    input_price_per_million: "2.50",
  });
  const original = structuredClone(item);
  const { user, requests, saved } = setup(item);
  const key = screen.getByLabelText("模型密钥");
  expect(key).toHaveValue("");
  await user.type(key, "fixture-edit-key-discard-on-clone");
  await user.click(screen.getByRole("button", { name: "另存为新预设" }));
  expect(key).toHaveValue("");
  expect(screen.getByLabelText("连接名称")).toHaveValue("日常预设 副本");
  expect(screen.getByRole("button", { name: "保存新预设" })).toBeDisabled();
  expect(requests).toHaveLength(0);
  await user.type(key, "fixture-new-preset-key");
  await user.click(screen.getByRole("button", { name: "保存新预设" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(requests[0].body).toMatchObject({
    name: "日常预设 副本",
    api_key: "fixture-new-preset-key",
    profile: {
      model: item.profile.model,
      default_effort: "low",
      context_window_tokens: 64000,
      max_output_tokens: 8192,
      input_price_per_million: 2.5,
    },
  });
  expect(requests[0].body).not.toHaveProperty("profile_id");
  expect(key).toHaveValue("");
  expect(item).toEqual(original);
  expect(JSON.stringify(requests)).not.toContain(
    "fixture-edit-key-discard-on-clone",
  );
  expect(Object.values(localStorage).join()).not.toContain(
    "fixture-new-preset-key",
  );
  expect(Object.values(sessionStorage).join()).not.toContain(
    "fixture-new-preset-key",
  );
  expect(document.body.textContent).not.toContain("fixture-new-preset-key");
});

test("decimal price strings from backend remain numeric on edit and blank stays unknown", async () => {
  const item = existing();
  Object.assign(item.profile, {
    input_price_per_million: "2.50",
    output_price_per_million: "10",
  });
  const { user, requests } = setup(item);
  await user.click(screen.getByText("高级选项：生成设置与费用"));
  expect(screen.getByLabelText("输入价格（USD / 百万 token）")).toHaveValue(
    2.5,
  );
  await user.clear(screen.getByLabelText("输出价格（USD / 百万 token）"));
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body.profile.input_price_per_million).toBe(2.5);
  expect(requests[0].body.profile.output_price_per_million).toBeNull();
});
