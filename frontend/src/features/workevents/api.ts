import type { LingApi } from "../../api/client";

/**
 * 工作事件（M2 阶段一）：事件索引、折叠开关与只读回查。
 *
 * 两件事必须分开：
 * - 模型输入折叠（folded）：后续模型请求里用短概况替换这段工作的细节。
 * - 界面展开原文：只读取已保留的记录，不经聊天或模型通路。
 *
 * 这里只调用 /api 下的 work-events 接口，不发送任何 chat/model 请求。
 */
export interface WorkEvent {
  id: string;
  agent_id?: string;
  conversation_id?: string;
  title: string;
  /** open / closed / invalidated；来源失效时服务端给 stale。 */
  status: string;
  /** 生命周期权威值，status 为 stale 时仍保留原值。 */
  lifecycle?: string | null;
  /** 执行与交付结果：pending / completed / cancelled / failed / partial。 */
  outcome?: string | null;
  /** 模型输入是否已折叠为概况。 */
  folded: boolean;
  /** 乐观并发版本；关闭、折叠、更正都带回当前值。 */
  revision: number;
  summary?: string | null;
  /** rule / model / owner：概况由谁生成。 */
  summary_kind?: string;
  coverage?: string | null;
  before_message_id?: string | null;
  start_message_id?: string | null;
  end_message_id?: string | null;
  source_count?: number;
  message_ids?: string[];
  /** 来源与分支此刻仍然有效。 */
  valid?: boolean;
  /** 服务端的失效或拒绝原因代码。 */
  reason?: string | null;
  created_at?: string;
  closed_at?: string | null;
}

export interface WorkEventRecord {
  id: string;
  source_id: string;
  kind: string;
  location: string;
  coverage: string;
  byte_count?: number | null;
  sha256?: string;
}

export interface WorkEventRecordPage {
  event_id?: string;
  revision: number;
  items: WorkEventRecord[];
  next_cursor?: string | null;
  coverage?: string | null;
}

export interface WorkEventRecordText {
  source_id: string;
  text: string;
  next_cursor?: string | null;
  location: string;
  coverage: string;
  sha256: string;
  byte_count?: number | null;
}

export interface WorkEventSummaryJob {
  id: string;
  status: string;
  /** done 时的处置结果：applied / rejected / rule_only / no_value。 */
  outcome?: string | null;
  detail?: string | null;
  error_code?: string | null;
  coverage?: string | null;
  /** 服务商返回的用量；缺失就是缺失，不推算。 */
  usage?: Record<string, unknown> | null;
  result_revision?: number | null;
  event_revision?: number;
  attempts?: number;
  max_calls?: number | null;
  profile_id?: string | null;
  cancel_requested?: boolean | number | null;
  created_at?: string;
  updated_at?: string;
}

export interface WorkEventSettings {
  enabled: boolean;
  /** 必须由所有者明确挑选；null 表示还没有选择，不会调用模型。 */
  profile_id: string | null;
  max_calls: number;
}

/**
 * 下一次请求的保守估算（本地组装，不发模型）。它是估算，不是服务商用量。
 */
export interface ContextPreview {
  available: boolean;
  reason?: string | null;
  profile_id?: string | null;
  context_limit?: number | null;
  output_reserve?: number | null;
  tool_schema_estimate?: number | null;
  estimated_total_with_output?: number | null;
  stored_message_bytes?: number | null;
  manifest?: {
    estimated_input_tokens?: number | null;
    truncated_messages?: number | null;
    work_events?: {
      event_id?: string;
      strategy?: string;
      replaced_messages?: number | null;
      sent?: boolean;
    }[];
    [key: string]: unknown;
  } | null;
  note?: string | null;
}

export function requestId() {
  // crypto.randomUUID 在旧环境缺失时退回本地唯一值，不影响请求语义。
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
    return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const segment = (value: string) => encodeURIComponent(value);

function query(values: Record<string, string | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== "") search.set(key, value);
  const text = search.toString();
  return text ? "?" + text : "";
}

export function workEventsApi(api: LingApi, conversationId: string) {
  const list = "/conversations/" + segment(conversationId) + "/work-events";
  const one = (eventId: string) => "/work-events/" + segment(eventId);
  return {
    list: (signal?: AbortSignal) => api.call<WorkEvent[]>(list, { signal }),
    start: (input: {
      title: string;
      request_id: string;
      start_message_id?: string;
    }) =>
      api.call<WorkEvent>(list, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    close: (eventId: string, expected_revision: number) =>
      api.call<WorkEvent>(one(eventId) + "/close", {
        method: "POST",
        body: JSON.stringify({ expected_revision }),
      }),
    fold: (eventId: string, expected_revision: number, folded: boolean) =>
      api.call<WorkEvent>(one(eventId) + "/fold", {
        method: "POST",
        body: JSON.stringify({ expected_revision, folded }),
      }),
    correctSummary: (
      eventId: string,
      expected_revision: number,
      text: string,
    ) =>
      api.call<WorkEvent>(one(eventId) + "/summary", {
        method: "POST",
        body: JSON.stringify({ expected_revision, text }),
      }),
    linkTask: (
      eventId: string,
      input: {
        task_id: string;
        expected_revision: number;
        origin_message_id?: string;
      },
    ) =>
      api.call<WorkEvent>(one(eventId) + "/tasks", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    records: (eventId: string, cursor: string | null, limit = 20) =>
      api.call<WorkEventRecordPage>(
        one(eventId) +
          "/records" +
          query({ cursor: cursor ?? undefined, limit: String(limit) }),
      ),
    record: (
      eventId: string,
      sourceId: string,
      cursor: string | null,
      maxBytes = 8192,
    ) =>
      api.call<WorkEventRecordText>(
        one(eventId) +
          "/records/" +
          segment(sourceId) +
          query({ cursor: cursor ?? undefined, max_bytes: String(maxBytes) }),
      ),
    summarize: (eventId: string, expected_revision: number) =>
      api.call<WorkEventSummaryJob>(one(eventId) + "/summarize", {
        method: "POST",
        body: JSON.stringify({ expected_revision }),
      }),
    jobs: (eventId: string, signal?: AbortSignal) =>
      api.call<WorkEventSummaryJob[]>(one(eventId) + "/summary-jobs", {
        signal,
      }),
    cancelJob: (eventId: string, jobId: string) =>
      api.call<WorkEventSummaryJob>(
        one(eventId) + "/summary-jobs/" + segment(jobId) + "/cancel",
        { method: "POST" },
      ),
  };
}

export type WorkEventsClient = ReturnType<typeof workEventsApi>;

export function workEventSettingsApi(api: LingApi, agentId: string) {
  const path = "/agents/" + segment(agentId) + "/work-event-settings";
  return {
    read: (signal?: AbortSignal) =>
      api.call<WorkEventSettings>(path, { signal }),
    save: (value: WorkEventSettings) =>
      api.call<WorkEventSettings>(path, {
        method: "PUT",
        body: JSON.stringify(value),
      }),
  };
}

export function contextPreviewApi(api: LingApi, conversationId: string) {
  const path = "/conversations/" + segment(conversationId) + "/context-preview";
  return {
    read: (profileId?: string | null, signal?: AbortSignal) =>
      api.call<ContextPreview>(
        path + query({ profile_id: profileId ?? undefined }),
        { signal },
      ),
  };
}

/** 事件在估算清单里的输入策略；未知取值原样显示。 */
export function strategyLabel(strategy?: string) {
  return (
    {
      folded: "折叠为概况",
      raw: "原始消息",
      receipt: "回查凭条",
      none: "未纳入",
    }[strategy ?? ""] ??
    (strategy || "未标注")
  );
}

/** 生命周期标签；未知取值原样显示，不替服务端编状态。 */
export function lifecycleLabel(event: WorkEvent) {
  if (event.status === "stale") return "来源已失效";
  const value = event.status;
  return (
    {
      open: "进行中",
      closed: "已结束",
      invalidated: "已失效",
    }[value] ?? value
  );
}

/** 执行与交付结果：closed 不等于成功，这里不做成功美化。 */
export function outcomeLabel(outcome?: string | null) {
  if (!outcome) return "结果未知";
  return (
    {
      pending: "结果未核验",
      running: "执行中",
      completed: "执行已结束",
      cancelled: "已取消",
      failed: "执行失败",
      partial: "含失败或未完成",
      unknown: "结果不确定",
      waiting: "等待确认",
      blocked: "受阻",
    }[outcome] ?? outcome
  );
}

export function outcomeTone(outcome?: string | null): "warn" | "ok" | "plain" {
  if (!outcome) return "plain";
  if (
    ["failed", "cancelled", "partial", "unknown", "blocked"].includes(outcome)
  )
    return "warn";
  if (["completed", "success", "passed"].includes(outcome)) return "ok";
  return "plain";
}

/** 来源覆盖：retained 才是完整保留，其余都要如实说明。 */
export function coverageLabel(coverage?: string | null) {
  if (!coverage) return "覆盖未知";
  return (
    {
      retained: "记录完整",
      capture_gap: "有采集缺口",
      bounded: "有界保留",
      redacted: "已脱敏",
      revoked: "来源已撤回",
      missing: "缺少记录",
    }[coverage] ?? coverage
  );
}

/** 来源有缺口：允许查看目录，但不能宣称原文完整。 */
export function hasGap(coverage?: string | null) {
  return !!coverage && coverage !== "retained";
}

export function reasonLabel(reason?: string | null) {
  if (!reason) return "";
  return (
    {
      permissions_changed: "目录授权已改变，无法继续读取这段工作的原文。",
      branch_changed: "当前分支已经离开这段工作，原文不再属于可见历史。",
      sources_changed: "这段工作的消息已被修改或删除，原记录已失效。",
      sources_unavailable:
        "这段工作的原文已无法读取，请检查本地记录是否仍存在。",
    }[reason] ?? reason
  );
}

export function summaryKindLabel(kind?: string) {
  return (
    {
      rule: "规则概况",
      model: "模型生成（未核验）",
      owner: "你的更正",
    }[kind ?? ""] ?? ""
  );
}

export function summaryJobLabel(status: string, outcome?: string | null) {
  if (status === "done")
    return (
      {
        applied: "已生成并启用（未核验）",
        rejected: "生成被拒绝，概况未启用",
        rule_only: "只生成规则概况，未采用模型文本",
        no_value: "模型没有给出可用概况",
      }[outcome ?? ""] ?? "已生成（未核验）"
    );
  return (
    {
      queued: "排队等待",
      running: "正在生成",
      retry: "等待重试",
      failed: "生成失败",
      stale: "概况已失效",
      cancelled: "已取消",
    }[status] ?? status
  );
}

/** 队列中或执行中才可以请求取消；终态作业不再显示取消入口。 */
export function jobRunning(status: string) {
  return ["queued", "running", "retry"].includes(status);
}

/** 服务商用量是可选返回；缺失时如实显示，不用 0 或推算值填充。 */
export function usageText(usage?: Record<string, unknown> | null) {
  if (!usage || !Object.keys(usage).length) return "服务商未返回用量";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(usage))
    if (value !== null && value !== undefined && typeof value !== "object")
      parts.push(key + "=" + String(value));
  return parts.length ? parts.join(" · ") : "服务商未返回用量";
}

export function jobErrorLabel(code?: string | null) {
  if (!code) return "";
  return (
    {
      provider_error: "模型服务返回错误",
      timeout: "模型请求超时",
      budget_exhausted: "已达到本次整理的调用上限",
      missing_credentials: "所选连接尚未提供密钥",
      summary_context_limit: "整理模型的上下文窗口不足，已保留原概况",
      credential_in_input: "来源包含当前整理模型凭据，已拒绝发送",
      call_budget: "已达到本次整理的调用上限",
      unknown: "失败原因未记录",
    }[code] ?? code
  );
}

export function recordKindLabel(kind: string) {
  return (
    {
      message: "消息",
      attachment: "附件",
      task: "任务",
      task_event: "任务记录",
      text: "正文",
      thinking: "思考",
      run_start: "运行开始",
      tool_call: "工具调用",
      tool_result: "工具结果",
      task_step: "任务步骤",
      task_review: "任务核对",
      guidance_applied: "已采纳的指导",
      guidance: "中途指导",
      step_reply: "步骤回复",
    }[kind] ?? kind
  );
}
