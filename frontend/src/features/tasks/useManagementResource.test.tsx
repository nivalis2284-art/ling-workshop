import { useCallback } from "react";
import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { LingApi } from "../../api/client";
import { backend } from "./test/fixtures";
import { useManagementResource } from "./useManagementResource";

afterEach(() => vi.useRealTimers());
function Probe({ api }: { api: LingApi }) {
  const load = useCallback(
    (signal: AbortSignal) =>
      api.call<{ text: string }>("/background", { signal }),
    [api],
  );
  const resource = useManagementResource(load);
  return <p>{resource.data?.text ?? "正在读取"}</p>;
}
test("五秒轮询在读取期间保留内容，卸载中止读取而不取消后台工作", async () => {
  vi.useFakeTimers();
  let number = 0;
  let finish: ((value: { text: string }) => void) | undefined;
  const { api, requests } = backend(() => {
    number++;
    if (number === 2)
      return new Promise<{ text: string }>((resolve) => {
        finish = resolve;
      });
    return { text: "真实状态" };
  });
  let view: ReturnType<typeof render>;
  await act(async () => {
    view = render(<Probe api={api} />);
  });
  expect(screen.getByText("真实状态")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4999);
  });
  expect(requests).toHaveLength(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(requests).toHaveLength(2);
  expect(screen.getByText("真实状态")).toBeInTheDocument();
  view!.unmount();
  expect(requests[1].options.signal?.aborted).toBe(true);
  await act(async () => {
    finish?.({ text: "迟到的返回" });
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(requests).toHaveLength(2);
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});
