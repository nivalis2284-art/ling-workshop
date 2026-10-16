import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { TaskChanges } from "./TaskChanges";
import { backend, task } from "./test/fixtures";

test("刷新后可读取持久写回状态，并单独审阅剩余文件后确认", async () => {
  const { api, requests } = backend(({ path }) => {
    if (path.endsWith("/application")) return {
      review_id: "old-review", status: "unknown", result: {
        review_id: "old-review", status: "unknown", effect_ids: [],
        files: [{path: "a.txt", status: "applied"}, {path: "b.txt", status: "unknown"}],
      },
    };
    if (path.endsWith("/reconcile")) return {
      review_id: "remaining-review", files: [{path: "b.txt", operation: "modify",
        before: "old", after: "new", diff: "-old\n+new", artifact: "b.txt"}],
    };
    if (path.endsWith("/apply")) return {
      review_id: "remaining-review", status: "applied", effect_ids: ["effect-b"],
    };
  });
  const user = userEvent.setup();
  render(<TaskChanges api={api} task={task} onApplied={async () => {}} />);
  await user.click(screen.getByRole("button", {name: "读取上次写回记录"}));
  expect(await screen.findByRole("list", {name: "持久写回记录"})).toHaveTextContent("a.txt：已应用");
  await user.click(screen.getByRole("button", {name: "核对并审阅剩余改动"}));
  expect(await screen.findByRole("heading", {name: "b.txt"})).toBeInTheDocument();
  expect(screen.queryByRole("heading", {name: "a.txt"})).not.toBeInTheDocument();
  expect(requests.filter(r => r.path.endsWith("/apply"))).toHaveLength(0);
  await user.click(screen.getByRole("button", {name: "应用这些改动"}));
  await user.click(screen.getByRole("button", {name: "确认写回项目"}));
  await screen.findByText("改动已写回项目。");
  expect(requests.filter(r => r.path.endsWith("/apply"))).toHaveLength(1);
});

test("成果通过鉴权二进制接口下载，保留字节与文件名", async () => {
  const { api, requests } = backend(({ path }) => {
    if (path.endsWith("/changes"))
      return {
        review_id: "r1",
        files: [
          {
            path: "输出/图 像.bin",
            operation: "create",
            before: null,
            after: "hash",
            diff: null,
            artifact: "输出/图 像.bin",
          },
        ],
      };
    if (
      path ===
      "/api/tasks/t1/artifacts/%E8%BE%93%E5%87%BA/%E5%9B%BE%20%E5%83%8F.bin"
    )
      return new Response(new Uint8Array([0, 255, 128, 42]), {
        headers: { "Content-Type": "application/octet-stream" },
      });
  });
  let downloaded: Blob | undefined;
  let filename = "";
  vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
    downloaded = blob as Blob;
    return "blob:fixture-artifact";
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    filename = this.download;
  });
  const user = userEvent.setup();
  render(<TaskChanges api={api} task={task} onApplied={async () => {}} />);
  await user.click(screen.getByRole("button", { name: "审阅文件改动" }));
  await user.click(
    await screen.findByRole("button", { name: "下载成果：输出/图 像.bin" }),
  );
  await waitFor(() => expect(downloaded).toBeDefined());
  expect(new Uint8Array(await downloaded!.arrayBuffer())).toEqual(
    new Uint8Array([0, 255, 128, 42]),
  );
  expect(filename).toBe("图 像.bin");
  const request = requests.find((r) => r.path.includes("/artifacts/"))!;
  expect(new Headers(request.options.headers).get("Authorization")).toBe(
    "Bearer fixture-owner-session",
  );
  expect(new Headers(request.options.headers).get("Accept")).toBe(
    "application/octet-stream",
  );
});

test("应用失败保留审阅内容和错误，不显示写回成功", async () => {
  const { api } = backend(({ path }) => {
    if (path.endsWith("/changes"))
      return {
        review_id: "r1",
        changes: [
          {
            path: "README.md",
            operation: "modify",
            before: "a",
            after: "b",
            diff: "+新内容",
            artifact: "README.md",
          },
        ],
      };
    if (path.endsWith("/apply"))
      return Response.json(
        { detail: "Original workspace changed since the task snapshot." },
        { status: 409 },
      );
  });
  const user = userEvent.setup();
  render(<TaskChanges api={api} task={task} onApplied={async () => {}} />);
  await user.click(screen.getByRole("button", { name: "审阅文件改动" }));
  await user.click(await screen.findByRole("button", { name: "应用这些改动" }));
  await user.click(screen.getByRole("button", { name: "确认写回项目" }));
  await waitFor(() =>
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent(
      "Original workspace changed",
    ),
  );
  expect(screen.queryByText("改动已写回项目。")).not.toBeInTheDocument();
  expect(screen.getByText("+新内容")).toBeInTheDocument();
});

test("部分写回逐文件显示结果且提供原始基线下载", async () => {
  const { api, requests } = backend(({ path }) => {
    if (path.endsWith("/changes"))
      return {
        review_id: "r-partial",
        changes: [
          {
            path: "a.txt",
            operation: "modify",
            before: "old-a",
            after: "new-a",
            diff: "-old-a\n+new-a",
            artifact: "a.txt",
            baseline_artifact: "__baseline__/a.txt",
          },
          {
            path: "b.txt",
            operation: "modify",
            before: "old-b",
            after: "new-b",
            diff: "-old-b\n+new-b",
            artifact: "b.txt",
            baseline_artifact: "__baseline__/b.txt",
          },
          {
            path: "c.txt",
            operation: "modify",
            before: "old-c",
            after: "new-c",
            diff: "-old-c\n+new-c",
            artifact: "c.txt",
            baseline_artifact: "__baseline__/c.txt",
          },
        ],
      };
    if (path.endsWith("/apply"))
      return {
        status: "unknown",
        review_id: "r-partial",
        effect_ids: ["effect-a"],
        files: [
          { path: "a.txt", status: "applied", effect_id: "effect-a" },
          { path: "b.txt", status: "unknown", reason: "effect_requires_reconciliation" },
          { path: "c.txt", status: "not_applied" },
        ],
      };
    if (path.endsWith("/artifacts/__baseline__/a.txt"))
      return new Response("old-a", {
        headers: { "Content-Type": "application/octet-stream" },
      });
  });
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:baseline");
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const user = userEvent.setup();
  render(<TaskChanges api={api} task={task} onApplied={async () => {}} />);
  await user.click(screen.getByRole("button", { name: "审阅文件改动" }));
  await user.click(await screen.findByRole("button", { name: "下载原始基线：a.txt" }));
  await user.click(await screen.findByRole("button", { name: "应用这些改动" }));
  await user.click(screen.getByRole("button", { name: "确认写回项目" }));
  const statusText = () =>
    screen.getAllByRole("status").map((element) => element.textContent).join(" ");
  await waitFor(() => expect(statusText()).toContain("写回状态：已应用"));
  expect(statusText()).toContain("结果未知，需人工核对");
  expect(statusText()).toContain("写回状态：未应用");
  expect(screen.getByRole("alert")).toHaveTextContent("不会自动重试");
  expect(requests.some((request) => request.path.includes("/__baseline__/"))).toBe(true);
  expect(screen.queryByRole("button", { name: "应用这些改动" })).toBeDisabled();
});
