import { expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../app/App";

const token = "management-test-owner";
const agent = { id: "ling-1", name: "小灵" };
const attachment = {
  id: "file-1",
  original_name: "资料.pdf",
  byte_count: 120,
  status: "partial",
  segments: [
    { location: "第 2 页", text: "可查阅的原文 <script>不是代码</script>" },
  ],
  metadata: { pages: 3 },
  warnings: ["部分页面含图片或未提取到文本，需要视觉模型进一步读取。"],
};
const usage = {
  scope: "chat",
  calls: 3,
  reported_calls: 2,
  input_tokens: null,
  output_tokens: 17,
  provider_total_tokens: null,
  cost_usd: null,
  cost_status: "unknown",
  note: "按服务商返回口径统计，缺少计价依据；当前查询覆盖聊天调用。",
};
function backend(
  options: {
    saveError?: number;
    uploadError?: number;
    readError?: number;
    unauthorizedDownload?: boolean;
  } = {},
) {
  let permissions = {
    agent_id: agent.id,
    revision: 4,
    readable: ["C:/资料"],
    writable: [] as string[],
  };
  let files: (typeof attachment)[] = [];
  const requests: {
    path: string;
    method: string;
    init: RequestInit;
    body: any;
  }[] = [];
  const json = (value: unknown, status = 200) =>
    new Response(JSON.stringify(value), { status });
  const fetcher = vi.fn(async (path: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const body =
      init.body instanceof FormData
        ? init.body
        : init.body
          ? JSON.parse(String(init.body))
          : null;
    requests.push({ path, method, init, body });
    if (path === "/api/agents")
      return json([agent, { id: "ling-2", name: "另一个灵" }]);
    if (path === "/api/models") return json([]);
    if (path === "/api/models/selection")
      return json({ primary: null, fallback: null });
    if (path === "/api/agents/ling-2/permissions")
      return json({
        agent_id: "ling-2",
        revision: 0,
        readable: [],
        writable: [],
      });
    if (path.endsWith("/permissions")) {
      if (method === "PUT") {
        if (options.saveError === 409) {
          permissions.revision = 5;
          return json({ detail: "权限版本冲突" }, 409);
        }
        if (options.saveError)
          return json({ detail: "请求内容无效" }, options.saveError);
        if (body.expected_revision !== permissions.revision)
          return json({ detail: "权限版本冲突" }, 409);
        permissions = {
          agent_id: agent.id,
          revision: permissions.revision + 1,
          readable: body.readable,
          writable: body.writable,
        };
      }
      return json(permissions);
    }
    if (path.endsWith("/usage")) return json(usage);
    if (path.endsWith("/instructions"))
      return json({
        agent_id: agent.id,
        rules: ["默认指令"],
        defaults: ["默认指令"],
        customized: false,
      });
    if (path === "/api/agents/ling-1/attachments") {
      if (method === "POST") {
        if (options.uploadError)
          return json({ detail: "服务端拒绝了此文件" }, options.uploadError);
        const file = body.get("file") as File;
        const result = { ...attachment, original_name: file.name };
        files = [result, ...files];
        return json(result, 201);
      }
      return json(files);
    }
    if (path === "/api/attachments/file-1")
      return options.readError
        ? json({ detail: "附件不存在" }, options.readError)
        : json(files[0]);
    if (path === "/api/attachments/file-1/download")
      return options.unauthorizedDownload
        ? json({}, 401)
        : new Response(new Uint8Array([0, 255, 3]), {
            headers: { "Content-Type": "application/octet-stream" },
          });
    throw new Error("Unexpected endpoint: " + method + " " + path);
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    requests,
    files: (value: (typeof attachment)[]) => {
      files = value;
    },
    options,
  };
}
async function open(page: "设置" | "资料") {
  const user = userEvent.setup();
  render(<App initialToken={token} />);
  await user.click(await screen.findByRole("button", { name: page }));
  return user;
}
function authenticated(requests: ReturnType<typeof backend>["requests"]) {
  for (const request of requests) {
    expect(new Headers(request.init.headers).get("Authorization")).toBe(
      "Bearer " + token,
    );
    expect(request.init.credentials).toBe("omit");
    expect(request.path.startsWith("/api/")).toBe(true);
    expect(request.path).not.toContain("/chat");
  }
}

test("权限仅在用户保存时分别提交读写路径，并采用服务端的新版本再次保存", async () => {
  const server = backend();
  const user = await open("设置");
  const read = await screen.findByLabelText("可读取路径");
  await user.clear(read);
  await user.type(read, "C:/文档\nD:/共享");
  await user.type(screen.getByLabelText("可写入路径"), "E:/产物");
  expect(server.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "保存权限" }));
  expect(await screen.findByText(/权限已保存/)).toBeInTheDocument();
  expect(server.requests.find((r) => r.method === "PUT")?.body).toEqual({
    readable: ["C:/文档", "D:/共享"],
    writable: ["E:/产物"],
    expected_revision: 4,
  });
  await user.clear(screen.getByLabelText("可写入路径"));
  await user.click(screen.getByRole("button", { name: "保存权限" }));
  await waitFor(() =>
    expect(server.requests.filter((r) => r.method === "PUT")).toHaveLength(2),
  );
  expect(server.requests.filter((r) => r.method === "PUT")[1].body).toEqual({
    readable: ["C:/文档", "D:/共享"],
    writable: [],
    expected_revision: 5,
  });
  authenticated(server.requests);
});

test("路径错误保留草稿且不谎报保存成功", async () => {
  const server = backend({ saveError: 422 });
  const user = await open("设置");
  const read = await screen.findByLabelText("可读取路径");
  await user.clear(read);
  await user.type(read, "C:/不存在");
  await user.click(screen.getByRole("button", { name: "保存权限" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/路径/);
  expect(read).toHaveValue("C:/不存在");
  expect(screen.queryByText(/权限已保存/)).not.toBeInTheDocument();
  expect(server.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
});

test("版本冲突不自动重试覆盖，重新读取后用新版本保存", async () => {
  const server = backend({ saveError: 409 });
  const user = await open("设置");
  await user.type(await screen.findByLabelText("可写入路径"), "D:/草稿");
  await user.click(screen.getByRole("button", { name: "保存权限" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/重新读取/);
  expect(screen.getByLabelText("可写入路径")).toHaveValue("D:/草稿");
  expect(screen.getByRole("button", { name: "保存权限" })).toBeDisabled();
  expect(server.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  server.options.saveError = undefined;
  await user.click(screen.getByRole("button", { name: "重新读取权限" }));
  await waitFor(() =>
    expect(screen.getByLabelText("可写入路径")).toHaveValue(""),
  );
  await user.type(screen.getByLabelText("可写入路径"), "D:/确认");
  await user.click(screen.getByRole("button", { name: "保存权限" }));
  await screen.findByText(/权限已保存/);
  expect(
    server.requests.filter((r) => r.method === "PUT")[1].body.expected_revision,
  ).toBe(5);
});

test("用量仅展示聊天口径，缺失 token 与费用不会变成零或推算总数", async () => {
  const server = backend();
  await open("设置");
  const panel = await screen.findByRole("region", { name: "用量" });
  await within(panel).findByText(usage.note);
  expect(within(panel).getByLabelText("调用次数")).toHaveTextContent("3");
  expect(within(panel).getByLabelText("已报告用量的调用")).toHaveTextContent(
    "2",
  );
  expect(within(panel).getByLabelText("输入 tokens")).toHaveTextContent(
    "未提供",
  );
  expect(within(panel).getByLabelText("输出 tokens")).toHaveTextContent("17");
  expect(within(panel).getByLabelText("服务商总 tokens")).toHaveTextContent(
    "未提供",
  );
  expect(within(panel).getByLabelText("费用估算（USD）")).toHaveTextContent(
    "未知",
  );
  expect(panel).not.toHaveTextContent(/\$|0\.00/);
  authenticated(server.requests);
});

test.each(["资料.pdf", "资料.docx"])(
  "上传 %s 使用 multipart，保留来源位置与部分提取警告",
  async (name) => {
    const server = backend();
    const user = await open("资料");
    await user.upload(
      screen.getByLabelText("选择资料文件"),
      new File(["document"], name),
    );
    expect(server.requests.filter((r) => r.method === "POST")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "上传资料" }));
    await screen.findByText("已保存可查阅");
    const preview = await screen.findByRole("region", { name: "资料预览" });
    await within(preview).findByText(attachment.segments[0].text);
    expect(preview).toHaveTextContent("第 2 页");
    expect(preview).toHaveTextContent("部分提取");
    expect(preview).toHaveTextContent(attachment.warnings[0]);
    expect(preview.querySelector("script")).toBeNull();
    expect(screen.getByText(/尚未接入对话/)).toBeInTheDocument();
    const upload = server.requests.find((r) => r.method === "POST")!;
    expect(upload.body).toBeInstanceOf(FormData);
    expect(upload.body.get("file").name).toBe(name);
    expect(new Headers(upload.init.headers).has("Content-Type")).toBe(false);
    authenticated(server.requests);
  },
);

test("需要视觉识别的附件不宣称已理解图片，仍能查看元数据", async () => {
  const server = backend();
  server.files([
    {
      ...attachment,
      original_name: "扫描.png",
      status: "needs_vision",
      segments: [],
      warnings: [],
      metadata: { pages: 1 },
    },
  ]);
  const user = await open("资料");
  await user.click(await screen.findByRole("button", { name: /扫描.png/ }));
  const preview = await screen.findByRole("region", { name: "资料预览" });
  expect(
    await within(preview).findByText(/尚未识别图片内容/),
  ).toBeInTheDocument();
  expect(preview).toHaveTextContent("需要视觉识别");
  expect(preview).toHaveTextContent("pages");
});

test("超过25MB在发送前阻止上传，合规大小仍显示服务端拒绝", async () => {
  const server = backend({ uploadError: 413 });
  const user = await open("资料");
  const file = new File(["small"], "large.pdf");
  Object.defineProperty(file, "size", { value: 25 * 1024 * 1024 + 1 });
  await user.upload(screen.getByLabelText("选择资料文件"), file);
  expect(await screen.findByRole("alert")).toHaveTextContent("25 MB");
  expect(screen.getByRole("button", { name: "上传资料" })).toBeDisabled();
  expect(server.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  await user.upload(
    screen.getByLabelText("选择资料文件"),
    new File(["small"], "small.pdf"),
  );
  await user.click(screen.getByRole("button", { name: "上传资料" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("服务端拒绝");
  expect(screen.queryByText("已保存可查阅")).not.toBeInTheDocument();
});

test("附件预览错误不显示虚假空内容", async () => {
  const server = backend({ readError: 404 });
  server.files([attachment]);
  const user = await open("资料");
  await user.click(await screen.findByRole("button", { name: /资料.pdf/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("附件不存在");
  expect(
    screen.queryByText(attachment.segments[0].text),
  ).not.toBeInTheDocument();
});

test("下载原始二进制经过认证，下载后释放临时地址", async () => {
  const server = backend();
  server.files([attachment]);
  const create = vi.fn((_blob: Blob) => "blob:management-download");
  const revoke = vi.fn();
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = create;
      static revokeObjectURL = revoke;
    },
  );
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  const user = await open("资料");
  await user.click(await screen.findByRole("button", { name: /资料.pdf/ }));
  await user.click(await screen.findByRole("button", { name: "下载原文件" }));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  const link = click.mock.instances[0] as HTMLAnchorElement;
  expect(link.download).toBe("资料.pdf");
  expect(link.href).toBe("blob:management-download");
  expect(create.mock.calls[0][0]).toBeInstanceOf(Blob);
  expect(create.mock.calls[0][0].size).toBe(3);
  await waitFor(() =>
    expect(revoke).toHaveBeenCalledWith("blob:management-download"),
  );
  const download = server.requests.find((r) => r.path.endsWith("/download"))!;
  expect(new Headers(download.init.headers).get("Accept")).toBe(
    "application/octet-stream",
  );
  authenticated(server.requests);
});

test("下载遇到401使用统一会话失效流程", async () => {
  const server = backend({ unauthorizedDownload: true });
  server.files([attachment]);
  const user = await open("资料");
  await user.click(await screen.findByRole("button", { name: /资料.pdf/ }));
  await user.click(await screen.findByRole("button", { name: "下载原文件" }));
  expect(
    await screen.findByRole("heading", { name: "连接你的本地空间" }),
  ).toBeInTheDocument();
});

test("切换灵重新读取其权限和用量，旧草稿不会写到新灵", async () => {
  const server = backend();
  const user = await open("设置");
  await user.type(await screen.findByLabelText("可写入路径"), "D:/未保存");
  await user.selectOptions(screen.getByLabelText("切换灵"), "ling-2");
  await waitFor(() =>
    expect(screen.getByLabelText("可读取路径")).toHaveValue(""),
  );
  expect(screen.getByLabelText("可写入路径")).toHaveValue("");
  expect(
    server.requests.some((r) => r.path === "/api/agents/ling-2/usage"),
  ).toBe(true);
  expect(server.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
});
