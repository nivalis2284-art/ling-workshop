import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type {
  EpisodeArchive,
  MainConversation,
  WorkBranch,
  WorkEpisode,
  WorkGuidance,
  WorkTaskState,
} from "../../api/types";
import { continuationApi, continuityRequestId } from "./api";

type LoadState = "loading" | "ready" | "error";

/** The server owns the binding. Each request is abandoned when the agent or
 * the component changes, so a late response can never replace a newer choice. */
export function useMainConversation(api: LingApi, agentId: string) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [state, setState] = useState<{
    status: LoadState;
    value: MainConversation | null;
    error: string;
    busy: boolean;
  }>({ status: "loading", value: null, error: "", busy: false });
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const generation = useRef(0);
  const mutating = useRef(false);

  const read = useCallback(
    async (showLoading = true) => {
      if (mutating.current) return;
      controller.current?.abort();
      const next = new AbortController();
      controller.current = next;
      if (showLoading)
        setState((current) => ({ ...current, status: "loading", value: null, error: "" }));
      try {
        const value = await client.main(agentId, next.signal);
        if (!next.signal.aborted && mounted.current)
          setState({ status: "ready", value, error: "", busy: false });
      } catch (error) {
        if (!next.signal.aborted && mounted.current)
          setState((current) => ({
            ...current,
            status: "error",
            error: errorMessage(error),
            busy: false,
          }));
      }
    },
    [agentId, client],
  );

  useEffect(() => {
    mounted.current = true;
    generation.current++;
    void read();
    return () => {
      mounted.current = false;
      generation.current++;
      controller.current?.abort();
    };
  }, [read]);

  const bind = useCallback(
    async (conversationId: string | null) => {
      const current = state.value;
      if (!current || mutating.current) return null;
      mutating.current = true;
      const started = generation.current;
      controller.current?.abort();
      setState((value) => ({ ...value, busy: true, error: "" }));
      try {
        const value = await client.bindMain(
          agentId,
          conversationId,
          current.revision,
        );
        if (mounted.current && started === generation.current)
          setState({ status: "ready", value, error: "", busy: false });
        return started === generation.current ? value : null;
      } catch (error) {
        if (mounted.current && started === generation.current)
          setState((value) => ({
            ...value,
            busy: false,
            error: errorMessage(error),
          }));
        return null;
      } finally {
        mutating.current = false;
      }
    },
    [agentId, client, state.busy, state.value],
  );

  return { ...state, refresh: read, bind };
}

/** Work branches are small, owner-only records. Polling is deliberately slow
 * and stops while hidden. Running rows expose pause/resume through the existing
 * run controls in the branch chat; this store owns lifecycle actions only. */
export function useWorkBranches(api: LingApi, agentId: string) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [items, setItems] = useState<WorkBranch[]>([]);
  const [status, setStatus] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState("");
  const [detail, setDetail] = useState<{
    branchId: string;
    taskState: WorkTaskState | null;
    episodes: WorkEpisode[];
    archives: EpisodeArchive[];
    guidance: WorkGuidance[];
  } | null>(null);
  const [actionError, setActionError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const generation = useRef(0);
  const mutating = useRef(false);

  const read = useCallback(
    async (quiet = false) => {
      if (mutating.current) return;
      controller.current?.abort();
      const next = new AbortController();
      controller.current = next;
      if (!quiet) setStatus("loading");
      try {
        const value = await client.workBranches(agentId, next.signal);
        if (!next.signal.aborted && mounted.current) {
          setItems((current) => value.map((row) => {
            const newer = current.find((item) => item.id === row.id && item.revision > row.revision);
            return newer ?? row;
          }));
          setStatus("ready");
          setError("");
        }
      } catch (cause) {
        if (!next.signal.aborted && mounted.current) {
          setStatus("error");
          setError(errorMessage(cause));
        }
      }
    },
    [agentId, client],
  );

  useEffect(() => {
    mounted.current = true;
    generation.current++;
    void read();
    return () => {
      mounted.current = false;
      generation.current++;
      controller.current?.abort();
    };
  }, [read]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void read(true);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [items, read]);

  const replace = useCallback((value: WorkBranch) => {
    setItems((current) => {
      const found = current.some((item) => item.id === value.id);
      return found
        ? current.map((item) => (item.id === value.id ? value : item))
        : [value, ...current];
    });
  }, []);

  const act = useCallback(
    async (
      id: string,
      operation: () => Promise<WorkBranch>,
      inspectRun = false,
    ) => {
      if (mutating.current) return null;
      mutating.current = true;
      const started = generation.current;
      controller.current?.abort();
      setBusyId(id);
      setActionError("");
      try {
        if (inspectRun) {
          const work = items.find((item) => item.id === id);
          if (!work?.run_id) return null;
          const run = await api.chatRun(work.run_id);
          if (run.status === "running")
            throw new Error(
              "运行仍在活动；请等当前步骤结束，或先请求暂停。",
            );
        }
        const value = await operation();
        if (mounted.current && started === generation.current) replace(value);
        return mounted.current && started === generation.current ? value : null;
      } catch (cause) {
        if (mounted.current && started === generation.current) setActionError(errorMessage(cause));
        return null;
      } finally {
        mutating.current = false;
        if (mounted.current && started === generation.current) setBusyId("");
      }
    },
    [api, items, replace],
  );

  const history = useCallback(async (branchId: string) => {
    const controller = new AbortController();
    try {
      const [taskState, episodes, archives, guidance] = await Promise.all([
        client.taskState(branchId, controller.signal),
        client.episodes(branchId, controller.signal),
        client.archives(branchId, controller.signal),
        client.guidance(branchId, controller.signal),
      ]);
      if (mounted.current) setDetail({ branchId, taskState, episodes, archives, guidance });
      return { taskState, episodes, archives, guidance };
    } catch (cause) {
      if (mounted.current) setActionError(errorMessage(cause));
      return null;
    }
  }, [client]);

  return {
    items,
    status,
    error,
    actionError,
    busyId,
    detail,
    history,
    refresh: read,
    start: async (input: {
      request_id: string;
      objective: string;
      profile_id?: string;
      origin_message_id?: string;
    }) => {
      if (mutating.current) return null;
      mutating.current = true;
      const started = generation.current;
      controller.current?.abort();
      setActionError("");
      try {
        const value = await client.startWork(agentId, input);
        if (mounted.current && started === generation.current) replace(value);
        return mounted.current && started === generation.current ? value : null;
      } catch (cause) {
        if (mounted.current && started === generation.current) setActionError(errorMessage(cause));
        return null;
      } finally { mutating.current = false; }
    },
    guide: (id: string, text: string, requestId = continuityRequestId()) =>
      act(id, () => client.guide(id, text, requestId)),
    cancel: (id: string, expectedRevision: number) =>
      act(id, () => client.cancel(id, expectedRevision)),
    finish: (id: string, expectedRevision: number) =>
      act(id, () => client.finish(id, expectedRevision), true),
    wake: (id: string, expectedRevision: number) =>
      act(id, () => client.wake(id, expectedRevision)),
  };
}

export type MainConversationState = ReturnType<typeof useMainConversation>;
export type WorkBranchesState = ReturnType<typeof useWorkBranches>;
