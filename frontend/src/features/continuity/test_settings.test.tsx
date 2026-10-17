import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { LingApi } from "../../api/client";
import type { CompactionSettings as CompactionPolicy, ReflectionPolicy, ReflectionQualification, ReflectionRun } from "../../api/types";
import { ReflectionSettings } from "./ReflectionSettings";
import { CompactionSettings } from "./CompactionSettings";

const agent = { id: "ling", name: "灵" };
const profile = { id: "mock", name: "整理连接", credentials_available: true, profile: { model: "fixture", api_family: "openai_chat_completions", base_url: "https://fixture.invalid", capabilities: { tools: true, streaming: true, thinking: false, effort_levels: [] } } };
const defaultPolicy: ReflectionPolicy = { enabled: false, revision: 1, local_time: "04:00", timezone: "Asia/Tokyo", min_age_days: 3, threshold_bytes: 32768, profile_id: null, max_calls: 8, max_source_bytes: 131072, max_events: 6, daily_call_limit: 24 };
const compaction: CompactionPolicy = { enabled: false, revision: 0, profile_modes: {}, trigger_ratio: .8, target_ratio: .5, max_summary_bytes: 16384, max_epochs: 16, validation: "offline_only", native_compaction_available: false };
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }

function backend(options: { enabled?: boolean; runs?: ReflectionRun[]; saveConflict?: boolean } = {}) {
  let policy = { ...defaultPolicy, ...(options.enabled ? { enabled: true, profile_id: "mock" } : {}) };
  let compactionPolicy = { ...compaction };
  let runs = options.runs ?? [];
  let qualification: ReflectionQualification = { profile_id: "mock", revision: 0, qualified: false, sample_count: 0, acceptable_count: 0, critical_errors: 0, evidence: "" };
  const requests: { path: string; method: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, "http://localhost");
    const path = url.pathname;
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path: path + url.search, method, body });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer settings-fixture");
    if (path === "/api/models") return json([profile]);
    if (path.endsWith("/reflection-settings")) {
      if (method === "PUT") {
        if (options.saveConflict) return json({ detail: "设置版本已改变" }, 409);
        expect(body).not.toHaveProperty("revision");
        expect(body.expected_revision).toBe(policy.revision);
        const { expected_revision: revision, ...fields } = body;
        policy = { ...fields, revision: revision + 1 };
      }
      return json(policy);
    }
    if (path.endsWith("/reflection-preview")) return json({ gate_passed: false, source_units: 2, eligible_units: 2, candidate_bytes: 100, threshold_bytes: 32768, cutoff: "2026-10-14T00:00:00Z", model_calls: 0, deferred: [{ reason: "oversize" }] });
    if (path.endsWith("/reflection-runs")) {
      if (method === "POST") { const job = { id: "job", status: "skipped", error_code: "below_gate", request_id: body.request_id, revision: 1, created_at: "2026-10-17T04:00:00Z", calls_used: 0 }; runs = [job]; return json(job); }
      return json(runs);
    }
    if (path === "/api/reflection-runs/job/cancel") { const job = { ...runs[0], status: "cancelled", cancel_requested: true }; runs = [job]; return json(job); }
    if (path === "/api/reflection-runs/job/publish") {
      expect(body.expected_revision).toBe(3);
      const job = { ...runs[0], status: "published", revision: 4 };
      runs = [job]; return json(job);
    }
    if (path.endsWith("/reflection-qualification")) {
      if (method === "PUT") {
        expect(body.expected_revision).toBe(qualification.revision);
        const { expected_revision: revision, ...fields } = body;
        qualification = { ...fields, revision: revision + 1 };
      }
      return json(qualification);
    }
    if (path.endsWith("/compaction-settings")) {
      if (method === "PUT") {
        expect(body).not.toHaveProperty("validation");
        expect(body).not.toHaveProperty("revision");
        const { expected_revision: revision, ...fields } = body;
        compactionPolicy = { ...fields, revision: revision + 1, validation: "offline_only", native_compaction_available: false };
      }
      return json(compactionPolicy);
    }
    if (path.endsWith("/main-conversation")) return json({ conversation: { id: "main" }, revision: 1, choices: [] });
    if (path === "/api/conversations/main/messages") return json([
      { id: "u1", role: "user", parent_id: null, text: "我们讨论了旅行，而不是完成了预订" },
      { id: "a1", role: "assistant", parent_id: "u1", text: "只是讨论，尚未预订" },
      { id: "u2", role: "user", parent_id: "a1", text: "未在此事件中的另一个话题" },
    ]);
    throw new Error(`Unexpected ${method} ${path}`);
  }));
  return { requests, get policy() { return policy; }, get qualification() { return qualification; } };
}

function openReflection(options: Parameters<typeof backend>[0] = {}) {
  const server = backend(options);
  const user = userEvent.setup();
  render(<ReflectionSettings api={new LingApi("settings-fixture", () => {})} agent={agent} />);
  return { server, user };
}

test("reflection persists time, timezone, age, byte gate, model and budgets with revision CAS before allowing a run", async () => {
  const { server, user } = openReflection();
  await user.click(await screen.findByRole("checkbox", { name: /定时整理主干旧历史/ }));
  await user.selectOptions(screen.getByLabelText("整理模型"), "mock");
  const time = screen.getByLabelText("执行时间");
  await user.clear(time); await user.type(time, "05:30");
  const timezone = screen.getByLabelText(/时区/);
  await user.clear(timezone); await user.type(timezone, "Europe/Berlin");
  expect(screen.getByRole("button", { name: "立即运行一次" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "保存反思设置" }));
  await screen.findByText(/反思设置已保存/);
  expect(server.policy).toMatchObject({ enabled: true, profile_id: "mock", local_time: "05:30", timezone: "Europe/Berlin", revision: 2 });
  const save = server.requests.find((request) => request.method === "PUT")!;
  expect(save.body).toEqual({ expected_revision: 1, enabled: true, local_time: "05:30", timezone: "Europe/Berlin", min_age_days: 3, threshold_bytes: 32768, profile_id: "mock", max_calls: 8, max_source_bytes: 131072, max_events: 6, daily_call_limit: 24 });
  expect(server.requests.some((request) => request.method === "POST")).toBe(false);
  expect(screen.getByRole("button", { name: "立即运行一次" })).toBeEnabled();
});

test("local preview and low-gate run explicitly report zero calls without sending chat or model requests", async () => {
  const { server, user } = openReflection({ enabled: true });
  await user.click(await screen.findByRole("button", { name: "本地预览" }));
  const preview = await screen.findByRole("complementary", { name: "本地反思预览" });
  expect(within(preview).getByText("未达门槛")).toBeVisible();
  expect(within(preview).getByText("100 B")).toBeVisible();
  expect(within(preview).getByText("0")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "立即运行一次" }));
  expect(await screen.findByText(/本地闸门未达到，本次没有调用模型/)).toBeVisible();
  expect(server.requests.filter((request) => request.method === "POST").map((request) => request.path)).toEqual(["/api/agents/ling/reflection-preview", "/api/agents/ling/reflection-runs"]);
  expect(server.requests.some((request) => request.path.includes("/chat"))).toBe(false);
});

test("bad timezone and conflicting policy preserve edits and do not silently retry", async () => {
  const { server, user } = openReflection({ enabled: true, saveConflict: true });
  const input = await screen.findByLabelText(/时区/);
  await user.clear(input); await user.type(input, "Fake/Zone");
  await user.click(screen.getByRole("button", { name: "保存反思设置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("有效 IANA 时区");
  expect(server.requests.filter((request) => request.method === "PUT")).toHaveLength(0);
  await user.clear(input); await user.type(input, "UTC");
  await user.click(screen.getByRole("button", { name: "保存反思设置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("设置版本已改变");
  expect(input).toHaveValue("UTC");
  expect(server.requests.filter((request) => request.method === "PUT")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "立即运行一次" })).toBeDisabled();
});

test("queued reflection can be cancelled through the job owner route", async () => {
  const { server, user } = openReflection({ enabled: true, runs: [{ id: "job", request_id: "r", status: "queued", created_at: "2026-10-17T00:00:00Z" }] });
  await user.click(await screen.findByRole("button", { name: "取消作业" }));
  expect(await screen.findByText("已取消")).toBeVisible();
  expect(server.requests.find((request) => request.path.endsWith("/cancel"))).toMatchObject({ method: "POST", path: "/api/reflection-runs/job/cancel" });
});

test("owner corrects a candidate against local source, reviews it and publishes without calling the model", async () => {
  const { server, user } = openReflection({ enabled: true, runs: [{ id: "job", request_id: "r", status: "candidate", quality_status: "quality_unverified", revision: 3, created_at: "2026-10-17T00:00:00Z", candidate_events: [{ title: "旅行", summary: "预订完成", source_ids: ["turn:u1"] }] }] });
  await user.click(await screen.findByRole("button", { name: "审阅并发布" }));
  const review = screen.getByRole("region", { name: "审阅整理摘要" });
  expect(within(review).getByRole("button", { name: "发布这批经历" })).toBeDisabled();
  await user.click(within(review).getByRole("button", { name: "查看对应原文" }));
  expect(await within(review).findByText(/我们讨论了旅行，而不是完成了预订/)).toBeVisible();
  expect(within(review).queryByText(/未在此事件中的另一个话题/)).not.toBeInTheDocument();
  const summary = within(review).getByLabelText("摘要");
  await user.clear(summary); await user.type(summary, "讨论了旅行计划，尚未预订。");
  await user.click(within(review).getByRole("checkbox", { name: /我已审阅/ }));
  await user.click(within(review).getByRole("button", { name: "发布这批经历" }));
  expect(await screen.findByText(/这批经历已发布/)).toBeVisible();
  expect(server.requests.find((request) => request.path.endsWith("/publish"))?.body).toEqual({ expected_revision: 3, events: [{ title: "旅行", summary: "讨论了旅行计划，尚未预订。", source_ids: ["turn:u1"] }] });
  expect(server.requests.filter((request) => request.method === "POST")).toHaveLength(1);
});

test("profile qualification requires the owner's frozen-sample evidence and can be revoked without a model call", async () => {
  const { server, user } = openReflection({ enabled: true });
  await user.click(await screen.findByRole("button", { name: "整理质量与自动发布" }));
  const quality = screen.getByRole("region", { name: "整理质量确认" });
  const confirm = await within(quality).findByRole("button", { name: "确认这条连接可自动发布" });
  expect(confirm).toBeDisabled();
  const sample = within(quality).getByLabelText("已检查样本数");
  await user.clear(sample); await user.type(sample, "30");
  const acceptable = within(quality).getByLabelText("无需重大修改的样本数");
  await user.clear(acceptable); await user.type(acceptable, "26");
  await user.type(within(quality).getByLabelText("验证记录或证据"), "owner-reviewed-set-v1");
  expect(confirm).toBeDisabled();
  await user.clear(acceptable); await user.type(acceptable, "27");
  await user.click(confirm);
  await within(quality).findByText("你已确认可自动发布");
  expect(server.qualification).toMatchObject({ profile_id: "mock", qualified: true, sample_count: 30, acceptable_count: 27, critical_errors: 0, evidence: "owner-reviewed-set-v1", revision: 1 });
  await user.click(within(quality).getByRole("button", { name: "恢复为每次人工审阅" }));
  await waitFor(() => expect(server.qualification.qualified).toBe(false));
  expect(server.requests.some((request) => request.method === "POST")).toBe(false);
});

test("compaction uses per-profile mode, ordered ratios and budget CAS without enabling unverified native requests", async () => {
  const server = backend(); const user = userEvent.setup();
  render(<CompactionSettings api={new LingApi("settings-fixture", () => {})} agent={agent} />);
  const toggle = await screen.findByRole("checkbox", { name: /允许工作分支/ });
  await user.click(toggle);
  const select = screen.getByRole("combobox");
  expect(within(select).getByRole("option", { name: /原生压缩/ })).toBeDisabled();
  await user.selectOptions(select, "safe_epoch_restart");
  await user.click(screen.getByRole("button", { name: "保存压缩策略" }));
  expect(await screen.findByText(/压缩策略已保存/)).toBeVisible();
  expect(server.requests.find((request) => request.method === "PUT")?.body).toEqual({ expected_revision: 0, enabled: true, profile_modes: { mock: "safe_epoch_restart" }, trigger_ratio: .8, target_ratio: .5, max_summary_bytes: 16384, max_epochs: 16 });
  expect(server.requests.some((request) => request.method === "POST")).toBe(false);
  const target = screen.getByLabelText("目标比例");
  await user.clear(target); await user.type(target, "0.9");
  expect(screen.getByRole("button", { name: "保存压缩策略" })).toBeDisabled();
});
