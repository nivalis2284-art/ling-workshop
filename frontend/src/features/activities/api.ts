import type { LingApi } from "../../api/client";
import type { SourceRef } from "../../api/types";

export type ActivityKind =
  "reminder" | "file_watch" | "web_watch" | "topic_review";
export interface Activity {
  id: string;
  agent_id: string;
  kind: ActivityKind;
  title: string;
  target: string | null;
  revision: number;
  status: string;
  next_due_at: string;
  interval_seconds: number | null;
  last_outcome: string | null;
  last_error: string | null;
  last_checked_at: string | null;
  busy: boolean;
}
export interface WebOrigins {
  agent_id: string;
  revision: number;
  origins: string[];
}
export interface Notification {
  id: string;
  activity_id: string;
  agent_id: string;
  evidence_id: string;
  message: string;
  created_at: string;
}
export interface Evidence {
  id: string;
  activity_id: string;
  payload: Record<string, unknown>;
  source: SourceRef;
  observed_at: string;
}
export interface ActivityInput {
  agent_id: string;
  kind: ActivityKind;
  title: string;
  target?: string;
  due_at: string;
  interval_seconds: number | null;
}
export const kindLabels: Record<ActivityKind, string> = {
  reminder: "提醒",
  file_watch: "文件关注",
  web_watch: "网页关注",
  topic_review: "主题复查",
};
export const activityLabels: Record<string, string> = {
  active: "进行中",
  paused: "已暂停",
  cancelled: "已取消",
  completed: "已完成",
};
export const outcomeLabels: Record<string, string> = {
  unchanged: "没有变化",
  baseline: "已记录初始内容",
  changed: "发现变化",
  reminded: "已提醒",
  pending_investigation: "等待主题复查",
  error: "检查未完成",
  permission_revoked: "授权已撤销",
  discarded: "本次检查已失效",
};
export function activitiesApi(api: LingApi) {
  const agentPath = (id: string) => "/agents/" + encodeURIComponent(id);
  const itemPath = (id: string) => "/activities/" + encodeURIComponent(id);
  async function pages<T>(path: string, signal: AbortSignal) {
    const all: T[] = [];
    for (let offset = 0; !signal.aborted; offset += 100) {
      const batch = await api.call<T[]>(
        path +
          "?" +
          new URLSearchParams({ limit: "100", offset: String(offset) }),
        { signal },
      );
      all.push(...batch);
      if (batch.length < 100) break;
    }
    return all;
  }
  return {
    list: (id: string, signal: AbortSignal) =>
      pages<Activity>(agentPath(id) + "/activities", signal),
    notifications: (id: string, signal: AbortSignal) =>
      pages<Notification>(agentPath(id) + "/activity-notifications", signal),
    origins: (id: string, signal: AbortSignal) =>
      api.call<WebOrigins>(agentPath(id) + "/activity-web-origins", { signal }),
    saveOrigins: (id: string, origins: string[], revision: number) =>
      api.call<WebOrigins>(agentPath(id) + "/activity-web-origins", {
        method: "PUT",
        body: JSON.stringify({ origins, expected_revision: revision }),
      }),
    create: (input: ActivityInput) =>
      api.call<Activity>("/activities", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    control: (item: Activity, action: "pause" | "resume" | "cancel") =>
      api.call<Activity>(
        itemPath(item.id) + (action === "resume" ? "" : "/" + action),
        {
          method: action === "resume" ? "PATCH" : "POST",
          body: JSON.stringify({
            expected_revision: item.revision,
            ...(action === "resume" ? { status: "active" } : {}),
          }),
        },
      ),
    evidence: (id: string, signal: AbortSignal) =>
      pages<Evidence>(itemPath(id) + "/evidence", signal),
  };
}
export function watchOrigin(raw: string) {
  if (/[\\\s]/.test(raw))
    throw new Error("请输入完整的网站地址，不要包含空格。");
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("请输入以 http:// 或 https:// 开头的网页地址。");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.hash ||
    !url.hostname ||
    url.hostname.includes("*")
  )
    throw new Error(
      "网页地址必须是具体网站，且不能包含账号、密码、通配符或页内标记。",
    );
  return url.origin;
}
export function parseOrigins(text: string) {
  const origins = text
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean)
    .map((value) => {
      const origin = watchOrigin(value);
      const url = new URL(value);
      if (url.pathname !== "/" || url.search)
        throw new Error(
          "网站来源只填写协议、域名与端口，例如 https://example.com，不要填写页面路径。",
        );
      return origin;
    });
  if (origins.length > 100) throw new Error("最多允许 100 个网站来源。");
  return [...new Set(origins)];
}
export function defaultDue() {
  const date = new Date(Date.now() + 15 * 60 * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60 * 1000)
    .toISOString()
    .slice(0, 16);
}
