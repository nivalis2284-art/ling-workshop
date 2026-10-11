import type { LingApi } from "../../api/client";

export interface BackgroundResult {
  status: string;
  backup_created?: boolean;
  error_code?: string | null;
  [key: string]: unknown;
}
export interface BackgroundStatus {
  enabled: boolean;
  working: boolean;
  last_result: BackgroundResult | null;
  interval_seconds: number;
}
export const backgroundLabels: Record<string, string> = {
  paused: "后台已暂停，本次检查未开始。",
  foreground_busy: "正在处理前台工作，本次后台运行未开始。",
  busy: "后台已有工作正在运行。",
  unavailable: "后台暂时不可用。",
  ok: "已检查一次自动备份。",
  failed: "本次后台工作失败。",
};
export function backgroundApi(api: LingApi) {
  return {
    status: (signal: AbortSignal) =>
      api.call<BackgroundStatus>("/background", { signal }),
    configure: (enabled: boolean) =>
      api.call<BackgroundStatus>("/background", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    runOnce: () =>
      api.call<BackgroundResult>("/background/run-once", { method: "POST" }),
  };
}
