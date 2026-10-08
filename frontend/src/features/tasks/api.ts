import type { LingApi } from "../../api/client";

export interface Task {
  id: string;
  agent_id: string;
  objective: string;
  request_id: string;
  workspace: string | null;
  environment?: "python" | "node";
  status: string;
  step_count: number;
  step_limit: number;
  verified: boolean;
  final: string | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  dependency_candidate?: {manager:"pip"|"npm";packages:string[]};
}
export interface TaskEvent {
  id: number;
  task_id: string;
  type: string;
  data: Record<string, unknown>;
  created_at: string;
}
export interface FileChange {
  path: string;
  operation: string;
  before: string | null;
  after: string | null;
  diff: string | null;
  artifact: string | null;
}
export interface ChangeReview {
  review_id: string | null;
  changes: FileChange[];
}
export interface ApplyResult {
  status: string;
  review_id: string;
  effect_ids: string[];
}
const path = (id: string) => "/tasks/" + encodeURIComponent(id);
export const taskLabels: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  waiting_owner: "需确认",
  paused: "已暂停",
  blocked: "需要处理",
  interrupted: "执行已中断",
  completed: "已完成",
  needs_review: "结果待核对",
  cancelled: "已取消",
};
export function tasksApi(api: LingApi) {
  return {
    list: (agent: string, signal: AbortSignal) =>
      api.call<Task[]>(
        "/tasks?" + new URLSearchParams({ agent_id: agent, limit: "1000" }),
        { signal },
      ),
    submit: (value: {
      agent_id: string;
      objective: string;
      request_id: string;
      workspace?: string;
      environment?: "python" | "node";
    }) =>
      api.call<Task>("/tasks", { method: "POST", body: JSON.stringify(value) }),
    read: (id: string, signal: AbortSignal) =>
      api.call<Task>(path(id), { signal }),
    events: async (id: string, signal: AbortSignal) => {
      const events: TaskEvent[] = [];
      let after = 0;
      while (!signal.aborted) {
        const batch = await api.call<TaskEvent[]>(
          path(id) +
            "/events?" +
            new URLSearchParams({ after: String(after), limit: "1000" }),
          { signal },
        );
        events.push(...batch);
        if (batch.length < 1000) break;
        const cursor = batch.at(-1)!.id;
        if (cursor <= after)
          throw new Error("事件记录未能继续读取，请重新加载。");
        after = cursor;
      }
      return events;
    },
    control: (
      id: string,
      action: "pause" | "resume" | "cancel",
      additional_steps = 0,
    ) =>
      api.call<Task>(path(id) + "/" + action, {
        method: "POST",
        ...(action === "resume"
          ? { body: JSON.stringify({ additional_steps }) }
          : {}),
      }),
    changes: async (id: string) => {
      const value = await api.call<{
        review_id: string | null;
        files?: FileChange[];
        changes?: FileChange[];
      }>(path(id) + "/changes");
      return {
        review_id: value.review_id,
        changes: value.changes ?? value.files ?? [],
      };
    },
    apply: (id: string, review_id: string) =>
      api.call<ApplyResult>(path(id) + "/apply", {
        method: "POST",
        body: JSON.stringify({ review_id }),
      }),
    artifact: (id: string, name: string) =>
      api.binary(
        path(id) +
          "/artifacts/" +
          name.split("/").map(encodeURIComponent).join("/"),
      ),
  };
}
export function downloadArtifact(blob: Blob, path: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = path.split(/[\\/]/).at(-1) || "任务成果";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
