import { expect, test } from "vitest";
import { waitFor } from "@testing-library/react";
import type { LingApi } from "../../api/client";
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

function apiFor(stream: Response, calls: string[]): LingApi {
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

test("暂停、恢复与中途指导调用对应接口并即时反映", async () => {
  const calls: string[] = [];
  const { response, send } = openStream();
  const session = await startTurn(response, send, calls);
  send(frame(2, "paused", { run_id: "run-1" }));
  await waitFor(() =>
    expect(session.getSnapshot().turns.at(-1)?.paused).toBe(true),
  );
  await session.resumeRun();
  expect(calls).toContain("resume");
  expect(session.getSnapshot().turns.at(-1)?.paused).toBe(false);
  await session.pauseRun();
  expect(calls).toContain("pause");
  expect(session.getSnapshot().turns.at(-1)?.paused).toBe(true);
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
