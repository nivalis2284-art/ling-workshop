import { expect, test, vi } from "vitest";
import { LingApi } from "../api/client";
import { readChatStream } from "../features/chat/stream";
import type { ChatEvent } from "../api/types";

const request = {
  agent_id: "ling-1",
  message: "你好",
  profile_id: "primary-profile",
  request_id: "request-1",
};

test("public API calls preserve the local authenticated HTTP boundary for feature modules", async () => {
  const fetched = vi.fn(
    async () => new Response(JSON.stringify({ revision: 3 })),
  );
  vi.stubGlobal("fetch", fetched);
  const api = new LingApi("test-owner", () => {});
  const signal = new AbortController().signal;
  expect(
    await api.call<{ revision: number }>("/feature/action", {
      method: "PUT",
      body: JSON.stringify({ revision: 2 }),
      signal,
    }),
  ).toEqual({ revision: 3 });
  expect(fetched).toHaveBeenCalledWith(
    "/api/feature/action",
    expect.objectContaining({
      method: "PUT",
      body: '{"revision":2}',
      signal,
      credentials: "omit",
      cache: "no-store",
      headers: expect.objectContaining({
        Authorization: "Bearer test-owner",
        "Content-Type": "application/json",
      }),
    }),
  );
  await api.call("/feature/read");
  expect(fetched).toHaveBeenLastCalledWith(
    "/api/feature/read",
    expect.objectContaining({ credentials: "omit" }),
  );
});

test.each([
  "feature/action",
  "https://provider.invalid/action",
  "//provider.invalid/action",
  "",
])(
  "public API calls reject nonlocal paths %s before fetching",
  async (path) => {
    const fetched = vi.fn();
    vi.stubGlobal("fetch", fetched);
    await expect(
      new LingApi("test-owner", () => {}).call(path),
    ).rejects.toThrow();
    expect(fetched).not.toHaveBeenCalled();
  },
);

function streamResponse(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        // Single-byte chunks also split Chinese characters and CRLF boundaries.
        for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream; charset=utf-8" } },
  );
}

test("public binary downloads preserve encoded artifact paths, bytes, cancellation and owner authorization", async () => {
  const bytes = new Uint8Array([0, 1, 255, 128]);
  const fetched = vi.fn(
    async (_path: string, _init: RequestInit) =>
      new Response(bytes, {
        headers: { "Content-Type": "application/octet-stream" },
      }),
  );
  vi.stubGlobal("fetch", fetched);
  const signal = new AbortController().signal;
  const result = await new LingApi("test-owner", () => {}).binary(
    "/tasks/task-1/artifacts/report%2Fdata.bin",
    signal,
  );
  expect(result).toBeInstanceOf(Blob);
  expect(new Uint8Array(await result.arrayBuffer())).toEqual(bytes);
  expect(fetched).toHaveBeenCalledWith(
    "/api/tasks/task-1/artifacts/report%2Fdata.bin",
    expect.objectContaining({
      signal,
      credentials: "omit",
      cache: "no-store",
      headers: expect.objectContaining({
        Authorization: "Bearer test-owner",
        Accept: "application/octet-stream",
      }),
    }),
  );
  expect(fetched.mock.calls[0][0]).not.toContain("test-owner");
});

test.each([
  "tasks/task-1/artifacts/file",
  "https://provider.invalid/file",
  "//provider.invalid/file",
  "",
])(
  "public binary downloads reject nonlocal paths %s before fetching",
  async (path) => {
    const fetched = vi.fn();
    vi.stubGlobal("fetch", fetched);
    await expect(
      new LingApi("test-owner", () => {}).binary(path),
    ).rejects.toThrow();
    expect(fetched).not.toHaveBeenCalled();
  },
);

test("public binary downloads use the existing unauthorized handling", async () => {
  const unauthorized = vi.fn();
  vi.stubGlobal(
    "fetch",
    async () => new Response('{"detail":"unauthorized"}', { status: 401 }),
  );
  await expect(
    new LingApi("test-owner", unauthorized).binary(
      "/tasks/task-1/artifacts/file",
    ),
  ).rejects.toMatchObject({ status: 401 });
  expect(unauthorized).toHaveBeenCalledOnce();
});

test("chat requests use owner authorization, omit cookies and preserve replay identifiers", async () => {
  const requests: { path: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    requests.push({ path, init });
    return path.endsWith("/cancel") || path.endsWith("run%2F1")
      ? new Response('{"id":"run/1","status":"cancelled"}')
      : streamResponse(
          'id: 1\nevent: complete\ndata: {"run_id":"run/1","status":"completed","assistant_source":null}\n\n',
        );
  });
  const api = new LingApi("test-owner", () => {});
  const controller = new AbortController();
  await api.startChat(request, controller.signal);
  await api.chatEvents("run/1", "23", controller.signal);
  await api.cancelChat("run/1");
  expect(await api.chatRun("run/1", controller.signal)).toMatchObject({
    id: "run/1",
    status: "cancelled",
  });
  expect(requests.map((item) => item.path)).toEqual([
    "/api/chat/stream",
    "/api/chat/runs/run%2F1/events?after=23",
    "/api/chat/runs/run%2F1/cancel",
    "/api/chat/runs/run%2F1",
  ]);
  for (const { init } of requests) {
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer test-owner",
    );
  }
  expect(requests[0].init.method).toBe("POST");
  expect(JSON.parse(String(requests[0].init.body))).toEqual(request);
  expect(requests[0].init.signal).toBe(controller.signal);
  expect(new Headers(requests[0].init.headers).get("Accept")).toBe(
    "text/event-stream",
  );
  expect(requests[2].init.method).toBe("POST");
});

test("SSE parsing streams split UTF-8, multiline data and event ids independently of chunk boundaries", async () => {
  const events: ChatEvent[] = [];
  await readChatStream(
    streamResponse(
      ': heartbeat\r\n\r\nid: 1\r\nevent: started\r\ndata: {"run_id":"run-1"}\r\n\r\n' +
        'id: 2\nevent: thinking\ndata: {"run_id":"run-1","text":"想一想"}\n\n' +
        'id: 3\nevent: text\ndata: {"run_id":"run-1",\ndata: "text":"你好，灵"}\n\n' +
        'id: 4\nevent: usage\ndata: {"run_id":"run-1","usage":{"input_tokens":5,"output_tokens":4,"total_tokens":9,"reasoning_tokens":null}}\n\n' +
        'id: 5\nevent: complete\ndata: {"run_id":"run-1","status":"completed","assistant_source":{"id":"source-1","revision":1}}\n\n',
    ),
    (event) => events.push(event),
    new AbortController().signal,
  );
  expect(events).toEqual([
    { id: "1", event: "started", data: { run_id: "run-1" } },
    { id: "2", event: "thinking", data: { run_id: "run-1", text: "想一想" } },
    { id: "3", event: "text", data: { run_id: "run-1", text: "你好，灵" } },
    {
      id: "4",
      event: "usage",
      data: {
        run_id: "run-1",
        usage: {
          input_tokens: 5,
          output_tokens: 4,
          total_tokens: 9,
          reasoning_tokens: null,
        },
      },
    },
    {
      id: "5",
      event: "complete",
      data: {
        run_id: "run-1",
        status: "completed",
        assistant_source: { id: "source-1", revision: 1 },
      },
    },
  ]);
});

test("EOF without a terminal event is a disconnection, not a successful answer", async () => {
  const events: ChatEvent[] = [];
  await expect(
    readChatStream(
      streamResponse(
        'id: 7\nevent: text\ndata: {"run_id":"run-1","text":"部分回答"}\n\n',
      ),
      (event) => events.push(event),
      new AbortController().signal,
    ),
  ).rejects.toThrow();
  expect(events[0]).toMatchObject({ data: { text: "部分回答" } });
});

test("started preserves conversation membership and model switches are parsed across split bytes", async () => {
  const events: ChatEvent[] = [];
  await readChatStream(
    streamResponse(
      'id: 1\nevent: started\ndata: {"run_id":"run-1","conversation_id":"conversation-1"}\n\n' +
        'id: 2\nevent: model_switch\ndata: {"run_id":"run-1","from_profile":"primary","to_profile":"backup","restart":true}\n\n' +
        'id: 3\nevent: complete\ndata: {"run_id":"run-1","status":"completed","assistant_source":null}\n\n',
    ),
    (event) => events.push(event),
    new AbortController().signal,
  );
  expect(events[0].data).toMatchObject({ conversation_id: "conversation-1" });
  expect(events[1]).toMatchObject({
    event: "model_switch",
    data: { from_profile: "primary", to_profile: "backup", restart: true },
  });
});

test.each([
  "id: 1\nevent: text\ndata: {invalid}\n\n",
  'id: 1\nevent: text\ndata: {"run_id":"run-1","text":123}\n\n',
  'id: 1\nevent: text\ndata: {"text":"missing run"}\n\n',
  'id: 1\nevent: complete\ndata: {"run_id":"run-1","status":"running","assistant_source":null}\n\n',
])("malformed events do not enter the visible transcript", async (text) => {
  const events: ChatEvent[] = [];
  await expect(
    readChatStream(
      streamResponse(text),
      (event) => events.push(event),
      new AbortController().signal,
    ),
  ).rejects.toThrow();
  expect(events).toHaveLength(0);
});

test("aborting a subscriber closes its reader without making a cancel request", async () => {
  let closed = false;
  const response = new Response(
    new ReadableStream({
      cancel: () => {
        closed = true;
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
  const controller = new AbortController();
  const reading = readChatStream(response, () => {}, controller.signal);
  controller.abort();
  await expect(reading).rejects.toMatchObject({ name: "AbortError" });
  expect(closed).toBe(true);
});
