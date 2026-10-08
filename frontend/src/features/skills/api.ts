import type { LingApi } from "../../api/client";
import type { SourceRef } from "../../api/types";

export interface Skill {
  id: string;
  agent_id: string;
  name: string;
  description: string;
  instructions: string;
  revision: number;
  status: string;
  sources: SourceRef[];
  created_at: string;
}
export type SkillAction = "activate" | "reject" | "disable";
export interface SkillVersion {
  revision: number;
  instructions: string;
  status: string;
  sources: SourceRef[];
  created_at: string;
}
export const skillLabels: Record<string, string> = {
  candidate: "待审阅",
  active: "已启用",
  rejected: "已拒绝",
  disabled: "已停用",
  stale: "来源已失效",
};
export function skillsApi(api: LingApi) {
  return {
    list: (id: string, signal: AbortSignal) =>
      api.call<Skill[]>("/agents/" + encodeURIComponent(id) + "/skills", {
        signal,
      }),
    decide: (skill: Skill, action: SkillAction) =>
      api.call<Skill>("/skills/" + encodeURIComponent(skill.id) + "/decision", {
        method: "POST",
        body: JSON.stringify({ action, expected_revision: skill.revision }),
      }),
    versions: (id: string, signal: AbortSignal) =>
      api.call<SkillVersion[]>(
        "/skills/" + encodeURIComponent(id) + "/versions",
        { signal },
      ),
    restore: (skill: Skill, target_revision: number) =>
      api.call<Skill>("/skills/" + encodeURIComponent(skill.id) + "/restore", {
        method: "POST",
        body: JSON.stringify({
          target_revision,
          expected_revision: skill.revision,
        }),
      }),
  };
}
