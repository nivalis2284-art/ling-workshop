import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import type { Conversation } from "../../api/types";
import { useResource } from "../../shared/useResource";

/**
 * The conversation list is shared: the sidebar shows it while the chat page drives
 * it, so both read the same selection instead of each keeping its own.
 */
export function useConversations(
  api: LingApi,
  agentId: string,
  initialId?: string,
) {
  const choiceKey = "lingnexus.chat.v1.choice." + agentId;
  const [selectedId, setSelectedId] = useState<string | undefined>(() => {
    try {
      return sessionStorage.getItem(choiceKey) ?? initialId;
    } catch {
      return initialId;
    }
  });
  const [created, setCreated] = useState<Conversation>();
  const [creating, setCreating] = useState(false);
  const load = useCallback(
    (signal: AbortSignal) => api.conversations(agentId, signal),
    [api, agentId],
  );
  const resource = useResource(load);
  const saved = resource.status === "ready" ? resource.data : [];
  const list =
    created && !saved.some((value) => value.id === created.id)
      ? [created, ...saved]
      : saved;
  const choose = useCallback(
    (id: string) => {
      setSelectedId(id);
      try {
        sessionStorage.setItem(choiceKey, id);
      } catch {
        /* Optional storage. */
      }
    },
    [choiceKey],
  );
  const adopt = useCallback(
    (value: Conversation) => {
      setCreated(value);
      choose(value.id);
    },
    [choose],
  );
  return {
    list,
    selectedId,
    choose,
    adopt,
    creating,
    setCreating,
    loading: resource.status === "loading",
    error: resource.status === "error" ? resource.error : "",
    refresh: resource.refresh,
  };
}

export type ConversationList = ReturnType<typeof useConversations>;
