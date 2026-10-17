import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import { workEventsApi } from "./api";
import type { WorkEvent, WorkEventsClient } from "./api";

/**
 * 一段对话的工作事件列表。面板与聊天记录共用同一份：界面收起某段已折叠工作的
 * 原始消息时，用的就是这里的边界，不需要第二个请求。
 *
 * 读取失败保留已有快照并显示错误；不会把失败伪装成没有工作事件。
 */
export function useWorkEvents(
  api: LingApi,
  conversationId: string | undefined,
  branchKey = "",
) {
  const client: WorkEventsClient | null = useMemo(
    () => (conversationId ? workEventsApi(api, conversationId) : null),
    [api, conversationId],
  );
  const [events, setEvents] = useState<WorkEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const epoch = useRef(0);

  useEffect(() => {
    setEvents(null);
    setError("");
    setLoading(false);
    setLoaded(false);
    epoch.current++;
    return () => {
      epoch.current++;
    };
  }, [conversationId]);

  const refresh = useCallback(async () => {
    if (!client) return;
    const version = ++epoch.current;
    setLoading(true);
    try {
      const value = await client.list();
      if (version !== epoch.current) return;
      setEvents(value);
      setError("");
    } catch (cause) {
      // 只有真的读失败才清空：服务端暂时不可用时，界面宁可显示错误也不要
      // 假装这段对话没有工作事件。
      if (version === epoch.current) setError(errorMessage(cause));
    } finally {
      if (version === epoch.current) {
        setLoaded(true);
        setLoading(false);
      }
    }
  }, [client]);

  useEffect(() => {
    if (!conversationId) return;
    void refresh();
  }, [conversationId, branchKey, refresh, client]);

  const patch = useCallback((value: WorkEvent) => {
    setEvents((current) =>
      current
        ? current.map((item) => (item.id === value.id ? value : item))
        : [value],
    );
  }, []);

  return {
    client,
    events,
    setEvents,
    patch,
    refresh,
    loading,
    error,
    setError,
    loaded,
  };
}

export type WorkEventList = ReturnType<typeof useWorkEvents>;

/**
 * 界面上「已收起原始聊天」的消息集合。
 *
 * 只收起来源有效、已关闭且已折叠的工作；进行中、失效或未折叠的事件一律原样
 * 显示，避免让所有者以为原文丢了。这里只影响显示，不改变模型输入。
 */
export function collapsedMessageIds(
  events: WorkEvent[] | null,
  revealed: Set<string>,
) {
  const hidden = new Set<string>();
  const owner = new Map<string, WorkEvent>();
  for (const event of events ?? []) {
    const lifecycle = event.lifecycle ?? event.status;
    if (lifecycle !== "closed" || !event.folded) continue;
    if (event.valid === false || event.status === "stale") continue;
    for (const id of event.message_ids ?? []) {
      if (!revealed.has(event.id)) hidden.add(id);
      owner.set(id, event);
    }
  }
  return { hidden, owner };
}
