import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { backend } from "../tasks/test/fixtures";
import { CredentialCachePanel } from "./CredentialCachePanel";

test("确认后只清除密钥缓存，刷新配置且不把密钥读回页面", async () => {
  const { api, requests } = backend(({ path, method }) => {
    if (path === "/api/models/credentials/cache" && method === "DELETE")
      return { enabled: true, cached_profiles: 0, error: null };
  });
  const cleared = vi.fn();
  const user = userEvent.setup();
  render(<CredentialCachePanel api={api} onCleared={cleared} />);
  await user.click(screen.getByRole("button", { name: "清除模型密钥缓存" }));
  expect(requests).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "确认清除密钥缓存" }));
  await screen.findByText(/密钥缓存已清除/);
  expect(cleared).toHaveBeenCalledOnce();
  expect(requests[0].method).toBe("DELETE");
  expect(requests[0].body).toEqual({});
  expect(new Headers(requests[0].options.headers).get("Authorization")).toBe(
    "Bearer fixture-owner-session",
  );
});

test("清除失败时保留错误提示，不显示已清除", async () => {
  const { api } = backend(() =>
    Response.json({ detail: "缓存文件不可访问" }, { status: 409 }),
  );
  const cleared = vi.fn();
  const user = userEvent.setup();
  render(<CredentialCachePanel api={api} onCleared={cleared} />);
  await user.click(screen.getByRole("button", { name: "清除模型密钥缓存" }));
  await user.click(screen.getByRole("button", { name: "确认清除密钥缓存" }));
  await screen.findByRole("alert");
  expect(cleared).not.toHaveBeenCalled();
  expect(screen.queryByText(/密钥缓存已清除/)).not.toBeInTheDocument();
});
