import type { LingApi } from "../../api/client";
import type {
  CompactionSettings,
  Experience,
  ExperiencePage,
  ExperienceRecordPage,
  ExperienceRecordText,
  MainConversation,
  EpisodeArchive,
  ReflectionPolicy,
  ReflectionPreview,
  ReflectionRun,
  ReflectionCandidate,
  ReflectionQualification,
  WorkBranch,
  WorkEpisode,
  WorkGuidance,
  WorkTaskState,
} from "../../api/types";

const segment = (value: string) => encodeURIComponent(value);

function query(values: Record<string, string | number | undefined | null>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "")
      search.set(key, String(value));
  }
  const text = search.toString();
  return text ? "?" + text : "";
}

export function continuityRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
    return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const agent = (id: string) => "/agents/" + segment(id);

export function continuationApi(api: LingApi) {
  return {
    main: (agentId: string, signal?: AbortSignal) =>
      api.call<MainConversation>(agent(agentId) + "/main-conversation", {
        signal,
      }),
    bindMain: (
      agentId: string,
      conversationId: string | null,
      expectedRevision: number,
    ) =>
      api.call<MainConversation>(agent(agentId) + "/main-conversation", {
        method: "PUT",
        body: JSON.stringify({
          conversation_id: conversationId,
          expected_revision: expectedRevision,
        }),
      }),
    workBranches: (agentId: string, signal?: AbortSignal) =>
      api.call<WorkBranch[]>(agent(agentId) + "/work-branches", { signal }),
    startWork: (
      agentId: string,
      input: {
        request_id: string;
        objective: string;
        profile_id?: string;
        origin_message_id?: string;
      },
    ) =>
      api.call<WorkBranch>(agent(agentId) + "/work-branches", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    work: (id: string, signal?: AbortSignal) =>
      api.call<WorkBranch>("/work-branches/" + segment(id), { signal }),
    taskState: (id: string, signal?: AbortSignal) =>
      api.call<WorkTaskState | null>("/work-branches/" + segment(id) + "/task-state", { signal }),
    episodes: (id: string, signal?: AbortSignal) =>
      api.call<WorkEpisode[]>("/work-branches/" + segment(id) + "/episodes", { signal }),
    archives: (id: string, signal?: AbortSignal) =>
      api.call<EpisodeArchive[]>("/work-branches/" + segment(id) + "/archives", { signal }),
    guidance: (id: string, signal?: AbortSignal) =>
      api.call<WorkGuidance[]>("/work-branches/" + segment(id) + "/guidance", { signal }),
    guide: (id: string, text: string, requestId: string) =>
      api.call<WorkBranch>("/work-branches/" + segment(id) + "/guide", {
        method: "POST",
        body: JSON.stringify({ text, request_id: requestId }),
      }),
    cancel: (id: string, expectedRevision: number) =>
      api.call<WorkBranch>("/work-branches/" + segment(id) + "/cancel", {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      }),
    finish: (id: string, expectedRevision: number) =>
      api.call<WorkBranch>("/work-branches/" + segment(id) + "/finish", {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      }),
    wake: (id: string, expectedRevision: number) =>
      api.call<WorkBranch>("/work-branches/" + segment(id) + "/wake", {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      }),
    experiences: (
      agentId: string,
      kind?: string,
      cursor?: string | null,
      limit = 20,
      signal?: AbortSignal,
    ) =>
      api.call<ExperiencePage>(
        agent(agentId) +
          "/experiences" +
          query({ kind: kind ?? undefined, cursor, limit }),
        { signal },
      ),
    experienceRecords: (
      id: string,
      conversationId: string | null,
      cursor: string | null,
      limit = 20,
      signal?: AbortSignal,
    ) =>
      api.call<ExperienceRecordPage>(
        "/experiences/" +
          segment(id) +
          "/records" +
          query({ conversation_id: conversationId, cursor, limit }),
        { signal },
      ),
    experienceRecord: (
      id: string,
      sourceId: string,
      conversationId: string | null,
      cursor: string | null,
      maxBytes = 8192,
      signal?: AbortSignal,
    ) =>
      api.call<ExperienceRecordText>(
        "/experiences/" +
          segment(id) +
          "/records/" +
          segment(sourceId) +
          query({ conversation_id: conversationId, cursor, max_bytes: maxBytes }),
        { signal },
      ),
    revoke: (id: string, expectedRevision: number) =>
      api.call<Experience>("/experiences/" + segment(id) + "/revoke", {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      }),
    reflectionSettings: (agentId: string, signal?: AbortSignal) =>
      api.call<ReflectionPolicy>(agent(agentId) + "/reflection-settings", {
        signal,
      }),
    saveReflectionSettings: (agentId: string, policy: ReflectionPolicy) =>
      api.call<ReflectionPolicy>(agent(agentId) + "/reflection-settings", {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: policy.revision,
          enabled: policy.enabled,
          local_time: policy.local_time,
          timezone: policy.timezone,
          min_age_days: policy.min_age_days,
          threshold_bytes: policy.threshold_bytes,
          profile_id: policy.profile_id,
          max_calls: policy.max_calls,
          max_source_bytes: policy.max_source_bytes,
          max_events: policy.max_events,
          daily_call_limit: policy.daily_call_limit,
        }),
      }),
    reflectionPreview: (agentId: string, signal?: AbortSignal) =>
      api.call<ReflectionPreview>(agent(agentId) + "/reflection-preview", {
        method: "POST",
        signal,
      }),
    reflectionRuns: (agentId: string, signal?: AbortSignal) =>
      api.call<ReflectionRun[]>(agent(agentId) + "/reflection-runs", {
        signal,
      }),
    runReflection: (agentId: string, requestId: string) =>
      api.call<ReflectionRun>(agent(agentId) + "/reflection-runs", {
        method: "POST",
        body: JSON.stringify({ request_id: requestId }),
      }),
    cancelReflection: (_agentId: string, id: string) =>
      api.call<ReflectionRun>(
        "/reflection-runs/" + segment(id) + "/cancel",
        { method: "POST" },
      ),
    qualification: (agentId: string, profileId: string, signal?: AbortSignal) =>
      api.call<ReflectionQualification>(agent(agentId) + "/reflection-qualification" + query({ profile_id: profileId }), { signal }),
    saveQualification: (agentId: string, value: ReflectionQualification) =>
      api.call<ReflectionQualification>(agent(agentId) + "/reflection-qualification", {
        method: "PUT",
        body: JSON.stringify({ expected_revision: value.revision, profile_id: value.profile_id, qualified: value.qualified, sample_count: value.sample_count, acceptable_count: value.acceptable_count, critical_errors: value.critical_errors, evidence: value.evidence }),
      }),
    publishReflection: (id: string, expectedRevision: number, events: ReflectionCandidate[]) =>
      api.call<ReflectionRun>("/reflection-runs/" + segment(id) + "/publish", {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision, events: events.map(({ title, summary, source_ids }) => ({ title, summary, source_ids })) }),
      }),
    compactionSettings: (agentId: string, signal?: AbortSignal) =>
      api.call<CompactionSettings>(agent(agentId) + "/compaction-settings", {
        signal,
      }),
    saveCompactionSettings: (agentId: string, value: CompactionSettings) =>
      api.call<CompactionSettings>(agent(agentId) + "/compaction-settings", {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: value.revision,
          enabled: value.enabled,
          profile_modes: value.profile_modes,
          trigger_ratio: value.trigger_ratio,
          target_ratio: value.target_ratio,
          max_summary_bytes: value.max_summary_bytes ?? 16384,
          max_epochs: value.max_epochs ?? 16,
        }),
      }),
  };
}

export type ContinuationClient = ReturnType<typeof continuationApi>;

export function workStatusLabel(status: string) {
  return (
    {
      queued: "排队中",
      starting: "正在准备",
      running: "执行中",
      paused: "已暂停",
      waiting: "等待确认",
      finishing: "正在收尾",
      completed: "已完成",
      archived: "已归档",
      failed: "失败",
      cancelled: "已取消",
      unknown: "结果不确定",
      interrupted: "等待恢复或安全唤醒",
      pending_review: "等待核对",
    }[status] ?? status
  );
}

export function workCanOpen(work: WorkBranch) {
  return !["archived", "completed", "cancelled", "failed"].includes(work.status);
}

export function workIsClosed(work: WorkBranch) {
  return ["archived", "completed", "cancelled", "failed"].includes(work.status);
}

export function workCanCancel(work: WorkBranch) {
  return !workIsClosed(work);
}

export function workCanControlRun(work: WorkBranch) {
  return !!work.run_id && ["running", "finishing"].includes(work.status);
}

/** The server refuses guidance before these statuses become actionable again. */
export function workCanGuide(work: WorkBranch) {
  return (
    !workIsClosed(work) &&
    ![
      "unknown",
      "pending_review",
      "awaiting_approval",
      "finishing",
      "interrupted",
    ].includes(work.status)
  );
}

// Temporary UI guards until the lifecycle API exposes allowed_actions. The
// finish request re-reads the server run immediately before submitting.
export function workCanFinish(work: WorkBranch) {
  return (
    workCanCancel(work) &&
    !!work.run_id &&
    !work.finish_requested &&
    ["paused", "interrupted", "pending_review"].includes(work.status)
  );
}

export function workCanWake(work: WorkBranch) {
  if (!["archived", "dormant", "paused", "interrupted"].includes(work.status))
    return false;
  return true;
}

export function experienceKindLabel(kind: string) {
  return (
    {
      work_branch: "工作分支",
      main_event: "主干事件",
      work_event: "旧工作记录",
      legacy_work_event: "旧工作记录",
    }[kind] ?? kind
  );
}

export function reflectionStatusLabel(status: string) {
  return (
    {
      queued: "排队中",
      running: "正在整理",
      done: "已结束",
      completed: "已结束",
      skipped: "本轮已跳过",
      retry: "等待重试",
      candidate: "等待审阅",
      published: "已发布",
      failed: "失败",
      cancelled: "已取消",
      paused: "已暂停",
    }[status] ?? status
  );
}
