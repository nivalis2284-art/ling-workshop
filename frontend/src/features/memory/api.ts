import type { LingApi } from "../../api/client";
import type { SourceRef } from "../../api/types";

export type MemoryKind = "episodes" | "knowledge";
export type MemoryScope = "real" | "external";
export type MemoryStatus = "all" | "active" | "archived" | "invalidated";
export type MemoryAction = "archive" | "restore" | "delete";
export interface MemorySnapshot {
  id: string;
  agent_id: string;
  title?: string;
  summary?: string;
  statement?: string;
  kind?: string;
  scope: MemoryScope;
  revision: number;
  status: Exclude<MemoryStatus, "all">;
  sources: SourceRef[];
  created_at?: string;
}
export interface MemoryItem extends MemorySnapshot {
  source_valid: boolean;
  source_notice?: string | null;
  provenance: "derived" | "owner_assertion";
  corrections: {
    revision: number;
    actor: "owner";
    created_at: string;
    prior: MemorySnapshot;
    current: MemorySnapshot;
  }[];
}
export const statusLabels = {
  active: "使用中",
  archived: "已归档",
  invalidated: "依据已失效",
};
export const scopeLabels = { real: "真实经历", external: "外部资料" };
export const kindLabels: Record<string, string> = {
  fact: "事实记录",
  preference: "偏好",
  experience: "经验",
  hypothesis: "待验证的认识",
};
export function memoryApi(api: LingApi) {
  const path = (kind: MemoryKind, id: string) =>
    "/memory/" + kind + "/" + encodeURIComponent(id);
  return {
    items: (
      agent: string,
      kind: MemoryKind,
      scope: MemoryScope,
      status: MemoryStatus,
      query: string,
      offset: number,
      signal: AbortSignal,
    ) => {
      const params = new URLSearchParams({
        kind,
        scope,
        status,
        query,
        offset: String(offset),
        limit: "20",
      });
      return api.call<MemoryItem[]>(
        "/memory/" + encodeURIComponent(agent) + "/items?" + params,
        { signal },
      );
    },
    read: (kind: MemoryKind, id: string, signal?: AbortSignal) =>
      api.call<MemoryItem>(path(kind, id), { signal }),
    correct: (
      kind: MemoryKind,
      item: MemoryItem,
      changes: { title?: string; summary?: string; statement?: string },
    ) =>
      api.call<MemoryItem>(path(kind, item.id), {
        method: "PATCH",
        body: JSON.stringify({ expected_revision: item.revision, ...changes }),
      }),
    state: (kind: MemoryKind, item: MemoryItem, action: MemoryAction) =>
      api.call<
        MemoryItem | { id: string; revision: number; status: "deleted" }
      >(path(kind, item.id) + "/state", {
        method: "POST",
        body: JSON.stringify({ expected_revision: item.revision, action }),
      }),
  };
}
