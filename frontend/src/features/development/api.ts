import type { LingApi } from "../../api/client";
import type { SourceRef } from "../../api/types";

export interface Goal {
  id: string;
  agent_id: string;
  title: string;
  status: string;
  progress: string;
  revision: number;
  sources: SourceRef[];
  created_at: string;
}
export type GoalAction = "activate" | "pause" | "complete" | "abandon";
export const goalLabels: Record<string, string> = {
  proposed: "待启用",
  active: "进行中",
  paused: "已暂停",
  completed: "已完成",
  abandoned: "已放弃",
  needs_review: "来源待复查",
};
export function developmentApi(api: LingApi) {
  return {
    list: (id: string, signal: AbortSignal) =>
      api.call<Goal[]>("/agents/" + encodeURIComponent(id) + "/goals", {
        signal,
      }),
    recordSource: (agent_id: string, title: string, request_id: string) =>
      api.call<SourceRef>("/sources", {
        method: "POST",
        body: JSON.stringify({
          agent_id,
          content: "长期目标：" + title,
          request_id,
        }),
      }),
    propose: (
      agent_id: string,
      title: string,
      source: SourceRef,
      request_id: string,
    ) =>
      api.call<Goal>("/goals", {
        method: "POST",
        body: JSON.stringify({
          agent_id,
          title,
          sources: [{ id: source.id, revision: source.revision }],
          request_id,
        }),
      }),
    decide: (goal: Goal, action: GoalAction) =>
      api.call<Goal>("/goals/" + encodeURIComponent(goal.id) + "/decision", {
        method: "POST",
        body: JSON.stringify({ action, expected_revision: goal.revision }),
      }),
  };
}
