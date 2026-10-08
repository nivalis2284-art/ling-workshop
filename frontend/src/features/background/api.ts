import type { LingApi } from "../../api/client";

export interface BackgroundResult {
  status: string;
  job_id?: string;
  outcome?: string;
  attempts?: number;
  error_code?: string | null;
  activity?: {
    activity_id: string;
    outcome: string;
    error_code: string | null;
  };
  [key: string]: unknown;
}
export interface BackgroundStatus {
  enabled: boolean;
  working: boolean;
  queued: number;
  last_result: BackgroundResult | null;
  interval_seconds: number;
}
export interface BackgroundJob {
  id: string;
  agent_id: string;
  kind: string;
  status: string;
  created_at: string;
  attempts: number | null;
  outcome: string | null;
  error_code: string | null;
}
export const jobLabels: Record<string, string> = {
  queued: "等待处理",
  running: "处理中",
  completed: "已完成",
  failed: "处理失败",
  skipped: "已跳过",
  stale: "来源已失效",
  blocked_credentials: "等待模型配置",
};
export const backgroundLabels: Record<string, string> = {
  paused: "后台已暂停",
  foreground_busy: "正在处理前台工作，本次后台运行未开始。",
  busy: "后台已有工作正在运行。",
  idle: "目前没有待处理工作。",
  activity: "已执行一次活动检查",
  completed: "本次工作已完成",
  failed: "本次工作失败",
  skipped: "本次工作已跳过",
  stale: "来源已失效",
  blocked_credentials: "请先在设置中配置可用模型。",
};
export function backgroundApi(api: LingApi) {
  return {
    status: (signal: AbortSignal) =>
      api.call<BackgroundStatus>("/background", { signal }),
    jobs: (signal: AbortSignal) =>
      api.call<BackgroundJob[]>("/background/jobs?limit=100", { signal }),
    configure: (enabled: boolean) =>
      api.call<BackgroundStatus>("/background", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    runOnce: () =>
      api.call<BackgroundResult>("/background/run-once", { method: "POST" }),
    retry: (id: string) =>
      api.call<BackgroundResult>(
        "/background/jobs/" + encodeURIComponent(id) + "/retry",
        { method: "POST" },
      ),
  };
}
