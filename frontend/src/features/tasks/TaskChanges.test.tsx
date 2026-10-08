import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { TaskChanges } from "./TaskChanges";
import { backend, task } from "./test/fixtures";

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
