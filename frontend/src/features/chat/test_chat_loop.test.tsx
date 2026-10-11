import { expect, test } from "vitest";
import { waitFor } from "@testing-library/react";
import { ApiError, type LingApi } from "../../api/client";
import type { PendingConfirmation } from "../../api/types";
import { ChatSession } from "./session";

function frame(id: number, event: string, data: unknown) {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** A stream that stays open, so the turn is live while the test drives it. */
function openStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
    },
  });
  return {
    response: new Response(body, {
      headers: { "Content-Type": "text/event-stream" },
    }),
    send: (text: string) => controller.enqueue(new TextEncoder().encode(text)),
  };
}

function apiFor(
  stream: Response,
  calls: string[],
  overrides: Partial<LingApi> = {},
): LingApi {
  return {
    startChat: async () => stream,
    chatRun: async () => ({
      id: "run-1",
      agent_id: "ling-1",
      request_id: "req-1",
      profile_id: "p",
      status: "running",
      conversation_id: "conversation-1",
      user_message_id: "user-1",
      created_at: "2026-10-11T00:00:00Z",
      finished_at: null,
    }),
    conversationMessages: async () => [],
    conversationBranches: async () => [],
    // The REST list is the source of truth and carries the full arguments.
    chatConfirmations: async () => [
      {
        run_id: "run-1",
        fingerprint: "fp1",
        tool: "run_command",
        arguments: { argv: ["ls"] },
      },
    ],
    pauseChat: async (runId: string) => {
      calls.push("pause");
      return { run_id: runId, paused: true };
    },
    resumeChat: async (runId: string) => {
      calls.push("resume");
      return { run_id: runId, paused: false };
    },
    guideChat: async (runId: string, text: string) => {
      calls.push("guide:" + text);
      return { run_id: runId, queued: 1 };
    },
    decideChatConfirmation: async (
      runId: string,
      fingerprint: string,
      approved: boolean,
    ) => {
      calls.push(`decide:${fingerprint}:${approved}`);
      return { run_id: runId, fingerprint, approved };
    },
    cancelChat: async () => {
      throw new Error("unused");
    },
    ...overrides,
  } as unknown as LingApi;
}

async function startTurn(
  stream: Response,
  send: (text: string) => void,
  calls: string[],
) {
  const session = new ChatSession(apiFor(stream, calls), "ling-1");
  session.attach();
  session.setProfile("p");
  session.send("跑个任务", "p");
  send(frame(1, "started", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.runId).toBe("run-1"),
  );
  return session;
}

test("对话循环把步骤与工具调用记进当前回合", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  const session = await startTurn(response, send, calls);
  send(frame(2, "step_started", { run_id: "run-1", step: 1 }));
  send(
    frame(3, "tool_started", {
      run_id: "run-1",
      step: 1,
      name: "create_file",
      call_id: "c1",
      arguments: { path: "/a" },
    }),
  );
  send(
    frame(4, "tool_result", {
      run_id: "run-1",
      step: 1,
      name: "create_file",
      call_id: "c1",
      status: "completed",
      preview: "已写入",
    }),
  );
  send(frame(5, "step_started", { run_id: "run-1", step: 2 }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.step).toBe(2),
  );
  expect(session.getSnapshot().turns.at(-1)?.tools).toEqual([
    {
      step: 1,
      name: "create_file",
      call_id: "c1",
      arguments: { path: "/a" },
      status: "completed",
      preview: "已写入",
    },
  ]);
  session.detach();
});

test("暂停与恢复由后端事件确认，请求期间挡住重复点击", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  const session = await startTurn(response, send, calls);
  await session.pauseRun();
  // POST 返回只代表标志已置、边界未到：不能乐观显示已暂停。
  expect(calls).toContain("pause");
  expect(session.getSnapshot().turns.at(-1)?.paused).toBeFalsy();
  expect(session.getSnapshot().turns.at(-1)?.pausePending).toBe(true);
  // 请求未落地前重复点击不再发出第二个暂停。
  await session.pauseRun();
  expect(calls.filter((item) => item === "pause")).toHaveLength(1);
  send(frame(2, "paused", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.paused).toBe(true),
  );
  expect(session.getSnapshot().turns.at(-1)?.pausePending).toBe(false);
  await session.resumeRun();
  expect(calls).toContain("resume");
  // resumed 事件未到，界面必须仍显示暂停，不能乐观翻转。
  expect(session.getSnapshot().turns.at(-1)?.paused).toBe(true);
  await session.resumeRun();
  expect(calls.filter((item) => item === "resume")).toHaveLength(1);
  send(frame(3, "resumed", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.paused).toBe(false),
  );
  expect(await session.guideRun("改用另一个目录")).toBe(true);
  expect(calls).toContain("guide:改用另一个目录");
  expect(await session.guideRun("   ")).toBe(false);
  session.detach();
});

test("需要确认的副作用会浮出并等待所有者决定", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  const session = await startTurn(response, send, calls);
  send(
    frame(2, "tool_started", {
      run_id: "run-1",
      step: 1,
      name: "run_command",
      call_id: "c1",
      arguments: { argv: ["ls"] },
    }),
  );
  send(
    frame(3, "notice", {
      run_id: "run-1",
      code: "confirmation_required",
      fingerprint: "fp1",
      tool: "run_command",
      arguments: { argv: ["ls"] },
      message: "该操作有现实副作用，等待所有者确认。",
    }),
  );
  await waitFor(() =>
    expect(
      session.getSnapshot().turns.at(-1)?.confirmations?.[0]?.arguments,
    ).toEqual({ argv: ["ls"] }),
  );
  expect(session.getSnapshot().turns.at(-1)?.confirmations?.[0]).toMatchObject({
    fingerprint: "fp1",
    tool: "run_command",
  });
  await session.decideConfirmation("fp1", true);
  expect(calls).toContain("decide:fp1:true");
  expect(session.getSnapshot().turns.at(-1)?.confirmations).toEqual([]);
  session.detach();
});

test("决策与回读并发时，已决策的确认卡不会复活，新指纹仍会补入", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  // 回读停在半路，模拟“GET 已发出、决策尚未被服务端处理”的竞态窗口。
  let release!: (value: PendingConfirmation[]) => void;
  const inFlight = new Promise<PendingConfirmation[]>((resolve) => {
    release = resolve;
  });
  const session = new ChatSession(
    apiFor(response, calls, { chatConfirmations: () => inFlight }),
    "ling-1",
  );
  session.attach();
  session.setProfile("p");
  session.send("跑个任务", "p");
  send(frame(1, "started", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.runId).toBe("run-1"),
  );
  send(
    frame(2, "notice", {
      run_id: "run-1",
      code: "confirmation_required",
      fingerprint: "fp1",
      tool: "run_command",
      arguments: { argv: ["ls"] },
    }),
  );
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.confirmations).toHaveLength(1),
  );
  await session.decideConfirmation("fp1", true);
  expect(session.getSnapshot().turns.at(-1)?.confirmations).toEqual([]);
  // 决策完成后再放行旧回读：它仍带着 fp1（旧快照），也带着决策期间
  // 新出现的 fp2。旧快照不得复活 fp1，也不能抹掉或挡下 fp2。
  release([
    {
      run_id: "run-1",
      fingerprint: "fp1",
      tool: "run_command",
      arguments: { argv: ["ls"] },
    },
    {
      run_id: "run-1",
      fingerprint: "fp2",
      tool: "run_command",
      arguments: { argv: ["rm"] },
    },
  ]);
  await waitFor(() =>
    expect(
      session
        .getSnapshot()
        .turns.at(-1)?.confirmations?.map((item) => item.fingerprint),
    ).toEqual(["fp2"]),
  );
  session.detach();
});

test("同一确认的并发决策只提交一次，不产生 404 假错误", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  // 第二次对同一指纹的 POST 在真实后端是 404；mock 保持同样的行为，
  // 这样任何重复提交都会以 actionError 的形式暴露出来。
  const session = new ChatSession(
    apiFor(response, calls, {
      decideChatConfirmation: async (
        runId: string,
        fingerprint: string,
        approved: boolean,
      ) => {
        if (calls.includes(`decide:${fingerprint}:${approved}`))
          throw new ApiError(404, "该确认请求不存在或已处理。");
        calls.push(`decide:${fingerprint}:${approved}`);
        return { run_id: runId, fingerprint, approved };
      },
    }),
    "ling-1",
  );
  session.attach();
  session.setProfile("p");
  session.send("跑个任务", "p");
  send(frame(1, "started", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.runId).toBe("run-1"),
  );
  send(
    frame(2, "notice", {
      run_id: "run-1",
      code: "confirmation_required",
      fingerprint: "fp1",
      tool: "run_command",
      arguments: { argv: ["ls"] },
    }),
  );
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.confirmations).toHaveLength(1),
  );
  // 双击：第二个调用必须被已决策记账短路。
  await Promise.all([
    session.decideConfirmation("fp1", true),
    session.decideConfirmation("fp1", true),
  ]);
  expect(calls.filter((item) => item.startsWith("decide:fp1"))).toHaveLength(1);
  expect(session.getSnapshot().turns.at(-1)?.confirmations).toEqual([]);
  expect(session.getSnapshot().actionError).toBeUndefined();
  session.detach();
});

test("停止是终态：确认卡与暂停标记一并清空", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  const session = new ChatSession(
    apiFor(response, calls, {
      cancelChat: async () => ({
        id: "run-1",
        agent_id: "ling-1",
        request_id: "req-1",
        profile_id: "p",
        status: "cancelled",
        conversation_id: "conversation-1",
        user_message_id: "user-1",
        created_at: "2026-10-11T00:00:00Z",
        finished_at: "2026-10-11T00:01:00Z",
      }),
    }),
    "ling-1",
  );
  session.attach();
  session.setProfile("p");
  session.send("跑个任务", "p");
  send(frame(1, "started", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.runId).toBe("run-1"),
  );
  send(
    frame(2, "notice", {
      run_id: "run-1",
      code: "confirmation_required",
      fingerprint: "fp1",
      tool: "run_command",
      arguments: { argv: ["ls"] },
    }),
  );
  send(frame(3, "paused", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.paused).toBe(true),
  );
  expect(session.getSnapshot().turns.at(-1)?.confirmations).toHaveLength(1);
  await session.stop();
  const turn = session.getSnapshot().turns.at(-1);
  expect(turn?.status).toBe("stopped");
  expect(turn?.confirmations).toEqual([]);
  expect(turn?.paused).toBeFalsy();
  expect(turn?.pausePending).toBeFalsy();
  session.detach();
});
