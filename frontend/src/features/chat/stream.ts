import type { ChatEvent, SourceRef } from "../../api/types";
import { ChatStreamError } from "./errors";

function isSourceRef(value: unknown): value is SourceRef {
  return (
    !!value &&
    typeof value === "object" &&
    "id" in value &&
    typeof value.id === "string" &&
    "revision" in value &&
    typeof value.revision === "number" &&
    Number.isInteger(value.revision)
  );
}

function parseEvent(id: string, event: string, json: string): ChatEvent {
  let data: unknown;
  try {
    data = JSON.parse(json);
  } catch {
    throw new ChatStreamError("malformed_stream");
  }
  if (!/^\d+$/.test(id) || !data || typeof data !== "object")
    throw new ChatStreamError("malformed_stream");
  const value = data as Record<string, unknown>;
  if (typeof value.run_id !== "string" || !value.run_id)
    throw new ChatStreamError("malformed_stream");
  const run_id = value.run_id;
  switch (event) {
    case "started":
      if (
        (value.conversation_id !== undefined &&
          value.conversation_id !== null &&
          (typeof value.conversation_id !== "string" ||
            !value.conversation_id)) ||
        (value.user_message_id !== undefined &&
          value.user_message_id !== null &&
          (typeof value.user_message_id !== "string" || !value.user_message_id))
      )
        break;
      return {
        id,
        event,
        data: {
          run_id,
          ...(value.conversation_id !== undefined
            ? { conversation_id: value.conversation_id as string | null }
            : {}),
          ...(value.user_message_id !== undefined
            ? { user_message_id: value.user_message_id as string | null }
            : {}),
        },
      };
    case "model_switch":
      if (
        typeof value.from_profile === "string" &&
        value.from_profile &&
        typeof value.to_profile === "string" &&
        value.to_profile &&
        typeof value.restart === "boolean"
      )
        return {
          id,
          event,
          data: {
            run_id,
            from_profile: value.from_profile,
            to_profile: value.to_profile,
            restart: value.restart,
          },
        };
      break;
    case "notice":
      if (typeof value.code === "string" && value.code)
        return { id, event, data: { run_id, code: value.code } };
      break;
    case "text":
    case "thinking":
      if (typeof value.text === "string")
        return { id, event, data: { run_id, text: value.text } };
      break;
    case "usage":
      if (
        value.usage &&
        typeof value.usage === "object" &&
        !Array.isArray(value.usage)
      )
        return {
          id,
          event,
          data: { run_id, usage: value.usage as Record<string, unknown> },
        };
      break;
    case "complete":
      if (
        value.status === "completed" &&
        (value.assistant_source === null || isSourceRef(value.assistant_source))
      )
        return {
          id,
          event,
          data: {
            run_id,
            status: "completed",
            assistant_source: value.assistant_source,
          },
        };
      break;
    case "error":
      if (typeof value.code === "string" && typeof value.message === "string")
        return {
          id,
          event,
          data: { run_id, code: value.code, message: value.message },
        };
  }
  throw new ChatStreamError("malformed_stream");
}

// Fetch is used instead of EventSource so every subscription carries owner auth.
export async function readChatStream(
  response: Response,
  onEvent: (event: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  if (
    !response.headers
      .get("content-type")
      ?.toLowerCase()
      .startsWith("text/event-stream") ||
    !response.body
  )
    throw new ChatStreamError("content_type");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let id = "";
  let type = "";
  let data: string[] = [];
  let terminal = false;
  const abort = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener("abort", abort, { once: true });
  const line = (value: string) => {
    if (value === "") {
      if (data.length) {
        const event = parseEvent(id, type, data.join("\n"));
        onEvent(event);
        terminal = event.event === "complete" || event.event === "error";
      }
      id = "";
      type = "";
      data = [];
      return;
    }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    const raw = colon < 0 ? "" : value.slice(colon + 1);
    const content = raw.startsWith(" ") ? raw.slice(1) : raw;
    if (field === "id") id = content;
    if (field === "event") type = content;
    if (field === "data") data.push(content);
  };
  try {
    while (!terminal) {
      signal.throwIfAborted();
      const chunk = await reader.read();
      signal.throwIfAborted();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let end: number;
      while ((end = buffer.search(/[\r\n]/)) >= 0 && !terminal) {
        if (buffer[end] === "\r" && end === buffer.length - 1 && !chunk.done)
          break;
        const size = buffer.slice(end, end + 2) === "\r\n" ? 2 : 1;
        line(buffer.slice(0, end));
        buffer = buffer.slice(end + size);
      }
      if (chunk.done && !terminal) throw new ChatStreamError("unexpected_eof");
    }
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
