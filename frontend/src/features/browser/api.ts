import type { LingApi } from "../../api/client";

export interface OriginGrant {
  origin: string;
  allow_local: boolean;
}
export interface BrowserOrigins {
  agent_id: string;
  revision: number;
  grants: OriginGrant[];
}
export interface BrowserPageState {
  manual_login: boolean;
  session_id: string;
  url?: string;
  page_version?: string;
}
export interface BrowserState {
  agent_id: string;
  origins: BrowserOrigins;
  page: BrowserPageState | null;
}
export interface BrowserApproval {
  id: string;
  action_id: string;
  agent_id: string;
  intent: Record<string, unknown>;
  intent_hash: string;
  status: string;
  created_at: string;
  decided_at: string | null;
}
export interface BrowserAction {
  id: string;
  status: string;
  error_code: string | null;
  result: Record<string, unknown> | null;
}
export const actionLabels: Record<string, string> = {
  goto: "打开网页",
  read: "读取网页",
  fill: "填写内容",
  click: "点击",
  submit: "提交表单",
  send: "发送",
  publish: "发布",
};
export function browserApi(api: LingApi, id: string) {
  const path = "/agents/" + encodeURIComponent(id) + "/browser";
  return {
    origins: (signal: AbortSignal) =>
      api.call<BrowserOrigins>(path + "/origins", { signal }),
    state: (signal: AbortSignal) =>
      api.call<BrowserState>(path + "/state", { signal }),
    approvals: (signal: AbortSignal) =>
      api.call<BrowserApproval[]>(path + "/approvals", { signal }),
    saveOrigins: (grants: OriginGrant[], revision: number) =>
      api.call<BrowserOrigins>(path + "/origins", {
        method: "PUT",
        body: JSON.stringify({ grants, expected_revision: revision }),
      }),
    decide: (approval: BrowserApproval, decision: "approve" | "reject") =>
      api.call<BrowserAction>(
        path + "/approvals/" + encodeURIComponent(approval.id) + "/decision",
        {
          method: "POST",
          body: JSON.stringify({
            decision,
            expected_intent_hash: approval.intent_hash,
          }),
        },
      ),
    login: (url: string, origin_revision: number) =>
      api.call<BrowserPageState>(path + "/login", {
        method: "POST",
        body: JSON.stringify({ url, origin_revision, headed: true }),
      }),
    finishLogin: () =>
      api.call<{ manual_login: false; page: null }>(path + "/login", {
        method: "DELETE",
      }),
  };
}
export function exactOrigin(value: string) {
  if (/[\\\s]/.test(value)) throw new Error("网站来源不能包含空格或反斜线。");
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("请输入完整网站来源，例如 https://example.com。");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.hostname.includes("*") ||
    url.hostname.endsWith(".") ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  )
    throw new Error(
      "网站来源只包含协议、域名和端口，不能包含路径、账号或通配符。",
    );
  return url.origin;
}
export function loginUrl(value: string, origins: BrowserOrigins) {
  if (/[\\\s]/.test(value)) throw new Error("登录地址不能包含空格或反斜线。");
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("请输入完整的登录页面地址。");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    !origins.grants.some((grant) => grant.origin === url.origin)
  )
    throw new Error("请先为登录页面的网站来源添加授权。");
  return value;
}
export function browserResult(
  result: BrowserAction,
  decision: "approve" | "reject",
) {
  if (decision === "reject" && result.error_code === "owner_rejected")
    return "已拒绝这次操作。";
  const status =
    {
      completed: "操作已完成",
      prepared: "等待批准",
      executing: "操作正在执行",
      failed: "操作未执行或失败，请核对记录",
      unknown: "操作结果不确定，请先检查目标网站，避免重复操作",
    }[result.status] ?? "操作状态：" + result.status;
  return status + (result.error_code ? "（" + result.error_code + "）" : "");
}
