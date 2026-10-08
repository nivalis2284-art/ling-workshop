import { useEffect, useRef } from "react";
import { useResource } from "../../shared/useResource";

// Keeps the last successful read visible while shared useResource refreshes.
// Mounted pages own reads only; detaching never sends a background cancellation.
export function useManagementResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
) {
  const resource = useResource(load);
  const cache = useRef<{ load: typeof load; data?: T }>({ load });
  if (cache.current.load !== load) cache.current = { load };
  if (resource.status === "ready") cache.current.data = resource.data;
  const reading = useRef(false);
  reading.current = resource.status === "loading";
  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!reading.current) void resource.refresh();
    }, 5000);
    return () => window.clearInterval(interval);
  }, [resource.refresh]);
  return { ...resource, data: cache.current.data };
}
