import { useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "../../api/client";

export function useManagementAction(refresh?: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const locked = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function run(operation: () => Promise<unknown>) {
    if (locked.current) return false;
    locked.current = true;
    setBusy(true);
    setError(null);
    try {
      await operation();
      if (mounted.current) await refresh?.();
      return true;
    } catch (cause) {
      if (mounted.current) {
        setError(
          cause instanceof ApiError && cause.status === 409
            ? "资料已发生变化，或当前状态不允许这次操作。" +
                errorMessage(cause) +
                " 已重新读取，请核对后再操作。"
            : errorMessage(cause),
        );
        if (cause instanceof ApiError && cause.status === 409)
          await refresh?.();
      }
      return false;
    } finally {
      locked.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return { busy, error, run };
}
