import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../api/client";

type Resource<T> =
  | { status: "loading"; data?: never; error?: never }
  | { status: "ready"; data: T; error?: never }
  | { status: "error"; data?: never; error: string };

export function useResource<T>(load: (signal: AbortSignal) => Promise<T>) {
  const [resource, setResource] = useState<Resource<T>>({ status: "loading" });
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setResource({ status: "loading" });
    try {
      const data = await load(controller.signal);
      if (!controller.signal.aborted) setResource({ status: "ready", data });
    } catch (error) {
      if (!controller.signal.aborted)
        setResource({ status: "error", error: errorMessage(error) });
    }
  }, [load]);
  useEffect(() => {
    void refresh();
    return () => active.current?.abort();
  }, [refresh]);
  return { ...resource, refresh };
}
