import { vi } from "vitest";
import { LingApi } from "../../../api/client";

export const agent = { id: "a/1", name: "初见" };
export const stamp = "2026-10-09T08:00:00+00:00";
export interface RequestRecord {
  path: string;
  method: string;
  body: Record<string, unknown>;
  options: RequestInit;
}
export function backend(
  handler: (request: RequestRecord) => unknown | Promise<unknown>,
) {
  const requests: RequestRecord[] = [];
  const unauthorized = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, options: RequestInit = {}) => {
      const url = new URL(String(input), "http://localhost");
      const request = {
        path: url.pathname + url.search,
        method: options.method ?? "GET",
        body: typeof options.body === "string" ? JSON.parse(options.body) : {},
        options,
      };
      requests.push(request);
      const value = await handler(request);
      if (value instanceof Response) return value;
      if (value === undefined)
        throw new Error(
          "Unexpected fixture route: " + request.method + " " + request.path,
        );
      return Response.json(value);
    }),
  );
  return {
    api: new LingApi("fixture-owner-session", unauthorized),
    requests,
    unauthorized,
  };
}
export const task = {
  id: "t1",
  agent_id: agent.id,
  objective: "整理项目说明",
  request_id: "request-1",
  workspace: "H:/project",
  profile_id: "p1",
  docker_image: null,
  status: "paused",
  step_count: 2,
  step_limit: 40,
  verified: false,
  final: null,
  error_code: null,
  created_at: stamp,
  updated_at: stamp,
};
export const approval = {
  id: "ap1",
  action_id: "ba1",
  agent_id: agent.id,
  status: "pending",
  intent_hash: "a".repeat(64),
  created_at: stamp,
  decided_at: null,
  intent: {
    action: "submit",
    session_id: "session-1",
    page_version: "v1",
    origin_revision: 2,
    url: "https://example.com/report",
    selector: "#submit",
    snapshot_hash: "b".repeat(64),
    target: { tag: "button", text: "提交周报", href: null },
    form: {
      action: "https://example.com/send",
      method: "post",
      fields: [{ name: "report", value: "本周完成说明" }],
    },
  },
};
