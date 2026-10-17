import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { LingApi } from "../../api/client";
import type { Attachment, SavedModel } from "../../api/types";
import { ModelEditor } from "../settings/ModelEditor";
import { ChatHarness } from "./testHarness";

/** The model picker now lives in a popup at the composer's bottom-right. */
async function chooseModel(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
) {
  await user.click(screen.getByRole("button", { name: "对话模型与思考强度" }));
  await user.click(screen.getByRole("radio", { name }));
}

const file = (
  id: string,
  original_name: string,
  fields: Partial<Attachment> = {},
): Attachment => ({
  id,
  original_name,
  byte_count: 120,
  status: "ready",
  segments: [{ location: "paragraph:1", text: "fixture-extracted-text" }],
  metadata: {},
  warnings: [],
  ...fields,
});
const files = [
  file("doc", "说明.docx"),
  file("partial", "长文.txt", {
    status: "partial",
    warnings: ["超过提取限制，当前结果不完整。"],
  }),
  file("image", "图片.png", {
    status: "needs_vision",
    segments: [],
    metadata: { width: 10, height: 10 },
  }),
  file("scan", "扫描.pdf", {
    status: "needs_vision",
    segments: [],
    metadata: { pages_needing_vision: [1] },
    warnings: ["需要视觉模型进一步读取。"],
  }),
  file("text-pdf", "文字.pdf"),
];
const model: SavedModel = {
  id: "text",
  name: "文字模型",
  credentials_available: true,
  profile: {
    api_family: "openai_responses",
    model: "fixture",
    base_url: "https://fixture.invalid",
    capabilities: {
      streaming: true,
      tools: false,
      thinking: false,
      effort_levels: [],
    },
  },
};

function backend(
  options: {
    oldAttachments?: string[];
    uploadFails?: boolean;
    delayedUpload?: boolean;
    rejectStart?: boolean;
    omitDocFromList?: boolean;
  } = {},
) {
  const requests: {
    path: string;
    init: RequestInit;
    body: Record<string, unknown> | null;
  }[] = [];
  const saved = [...files];
  const oldUser = {
    id: "user",
    role: "user",
    text: "原问题",
    parent_id: null,
    ...(options.oldAttachments
      ? { attachment_ids: options.oldAttachments }
      : {}),
  };
  const messages = [
    oldUser,
    {
      id: "answer",
      role: "assistant",
      text: "原回复",
      parent_id: "user",
    },
  ];
  let releaseUpload: ((response: Response) => void) | undefined;
  const uploadGate = options.delayedUpload
    ? new Promise<Response>((resolve) => {
        releaseUpload = resolve;
      })
    : undefined;
  let rejectStart = !!options.rejectStart;
  const json = (value: unknown, status = 200) =>
    new Response(JSON.stringify(value), { status });
  const uploaded = file("upload", "上传.md", {
    status: "partial",
    warnings: ["上传文件仅提取了部分内容。"],
  });
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer attachment-owner",
    );
    const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
    requests.push({ path, init, body });
    if (path === "/api/models")
      return json([
        model,
        {
          ...model,
          id: "image-model",
          name: "图片模型",
          profile: {
            ...model.profile,
            capabilities: { ...model.profile.capabilities, images: true },
          },
        },
        {
          ...model,
          id: "pdf-model",
          name: "PDF 模型",
          profile: {
            ...model.profile,
            capabilities: {
              ...model.profile.capabilities,
              images: true,
              pdf: true,
            },
          },
        },
        {
          ...model,
          id: "chat-pdf",
          name: "Chat 接口",
          profile: {
            ...model.profile,
            api_family: "openai_chat_completions",
            capabilities: { ...model.profile.capabilities, pdf: true },
          },
        },
      ]);
    if (path === "/api/models/selection")
      return json({ primary: "text", fallback: null });
    if (path.endsWith("/main-conversation")) return json({ conversation: { id: "conversation", agent_id: "ling", title: "交流", scope: "real", head_id: "answer" }, revision: 1, choices: [] });
    if (path.endsWith("/work-branches")) return json([]);
    if (path === "/api/agents/ling/conversations")
      return json([
        {
          id: "conversation",
          agent_id: "ling",
          title: "交流",
          scope: "real",
          head_id: "answer",
        },
      ]);
    if (path === "/api/conversations/conversation/messages")
      return json(messages);
    if (path === "/api/conversations/conversation/branches")
      return json([{ ...messages[1], selected: true }]);
    if (path === "/api/agents/ling/attachments") {
      if (init.method === "POST") {
        expect(init.body).toBeInstanceOf(FormData);
        expect(new Headers(init.headers).has("Content-Type")).toBe(false);
        expect((init.body as FormData).get("file")).toBeInstanceOf(File);
        if (options.uploadFails)
          return json({ detail: "文件内容无法读取" }, 422);
        if (uploadGate) return uploadGate;
        saved.unshift(uploaded);
        return json(uploaded, 201);
      }
      return json(
        options.omitDocFromList
          ? saved.filter((item) => item.id !== "doc")
          : saved,
      );
    }
    if (path.startsWith("/api/attachments/"))
      return json(saved.find((value) => value.id === path.split("/").at(-1)));
    if (path === "/api/chat/stream") {
      if (rejectStart) {
        rejectStart = false;
        throw new TypeError("fixture-disconnected");
      }
      return new Response(new ReadableStream(), {
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    throw new Error("Unexpected fixture endpoint: " + path);
  });
  return {
    requests,
    uploaded,
    async release() {
      saved.unshift(uploaded);
      await act(async () => {
        releaseUpload!(json(uploaded, 201));
      });
    },
  };
}
function open() {
  const api = new LingApi("attachment-owner", () => {});
  return render(<ChatHarness api={api} />);
}
async function picker(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText("原回复");
  await user.click(screen.getByRole("button", { name: "添加附件" }));
  await screen.findByRole("checkbox", { name: "选择 说明.docx" });
}
async function send(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("消息"), "看看文件");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
}

test("saved document selections show names and partial warnings, post only IDs, and clear after send", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await picker(user);
  await user.click(screen.getByRole("checkbox", { name: "选择 说明.docx" }));
  await user.click(screen.getByRole("checkbox", { name: "选择 长文.txt" }));
  expect(screen.getByText("超过提取限制，当前结果不完整。")).toBeVisible();
  await send(user);
  const request = server.requests.find(
    (value) => value.path === "/api/chat/stream",
  )!.body!;
  expect(request).toEqual({
    agent_id: "ling",
    conversation_id: "conversation",
    profile_id: "text",
    message: "看看文件",
    request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    attachment_ids: ["doc", "partial"],
  });
  expect(
    screen.getByRole("checkbox", { name: "选择 说明.docx" }),
  ).not.toBeChecked();
  expect(
    screen.getByRole("checkbox", { name: "选择 长文.txt" }),
  ).not.toBeChecked();
  expect(
    screen.getByRole("checkbox", { name: "选择 说明.docx" }),
  ).toBeDisabled();
  expect(within(screen.getByRole("log")).getByText("说明.docx")).toBeVisible();
  expect(Object.values(sessionStorage).join(" ")).toContain(
    '"attachment_ids":["doc","partial"]',
  );
  expect(Object.values(sessionStorage).join(" ")).not.toMatch(
    /attachment-owner|fixture-extracted-text|fixture.invalid/,
  );
});

test("upload uses authenticated multipart, blocks send until accepted, and selects the returned attachment ID", async () => {
  const server = backend({ delayedUpload: true });
  const user = userEvent.setup();
  open();
  await picker(user);
  await user.type(screen.getByLabelText("消息"), "上传后发送");
  await user.upload(
    screen.getByLabelText("上传聊天附件"),
    new File(["# fixture"], "上传.md", { type: "text/markdown" }),
  );
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(
    server.requests.some((value) => value.path === "/api/chat/stream"),
  ).toBe(false);
  await server.release();
  expect(
    await screen.findByRole("checkbox", { name: "选择 上传.md" }),
  ).toBeChecked();
  expect(screen.getByText("上传文件仅提取了部分内容。")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  expect(
    server.requests.find((value) => value.path === "/api/chat/stream")!.body
      ?.attachment_ids,
  ).toEqual(["upload"]);
});

test("failed upload does not select a fabricated file or block a text-only send", async () => {
  const server = backend({ uploadFails: true });
  const user = userEvent.setup();
  open();
  await picker(user);
  await user.upload(
    screen.getByLabelText("上传聊天附件"),
    new File(["fixture"], "上传.md", { type: "text/markdown" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "文件内容无法读取",
  );
  expect(
    screen.queryByRole("checkbox", { name: "选择 上传.md" }),
  ).not.toBeInTheDocument();
  await send(user);
  expect(
    server.requests.find((value) => value.path === "/api/chat/stream")!.body,
  ).not.toHaveProperty("attachment_ids");
});

test("images and scanned PDFs require explicit declared capabilities while extracted PDF text remains usable", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await picker(user);
  await user.type(screen.getByLabelText("消息"), "分析附件");
  await user.click(screen.getByRole("checkbox", { name: "选择 图片.png" }));
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(screen.getByText(/当前模型未声明图片能力/)).toBeVisible();
  await chooseModel(user, "图片模型");
  expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();
  await user.click(screen.getByRole("checkbox", { name: "选择 扫描.pdf" }));
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(screen.getByText(/当前模型未声明 PDF 能力/)).toBeVisible();
  await chooseModel(user, "PDF 模型");
  expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();
  await user.click(screen.getByRole("checkbox", { name: "选择 图片.png" }));
  await user.click(screen.getByRole("checkbox", { name: "选择 扫描.pdf" }));
  await chooseModel(user, "文字模型");
  await user.click(screen.getByRole("checkbox", { name: "选择 文字.pdf" }));
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  expect(
    server.requests.find((value) => value.path === "/api/chat/stream")!.body
      ?.attachment_ids,
  ).toEqual(["text-pdf"]);
});

test.each([{ attachmentIds: [] }, { attachmentIds: ["doc"] }])(
  "regeneration reuses old user attachments $attachmentIds and keeps unrelated composer selections",
  async ({ attachmentIds: oldAttachments }) => {
    const server = backend({ oldAttachments });
    const user = userEvent.setup();
    open();
    await picker(user);
    await user.click(screen.getByRole("checkbox", { name: "选择 长文.txt" }));
    await user.click(screen.getByRole("button", { name: "重新生成" }));
    const request = server.requests.find(
      (value) => value.path === "/api/chat/stream",
    )!.body!;
    expect(request.regenerate_message_id).toBe("user");
    expect(request.message).toBe("原问题");
    expect(request.attachment_ids ?? []).toEqual(oldAttachments);
    expect(
      screen.getByRole("checkbox", { name: "选择 长文.txt" }),
    ).toBeChecked();
  },
);

test("attachment selection and pending POST IDs survive reload and reconnection sends the same request", async () => {
  const server = backend({ rejectStart: true });
  const user = userEvent.setup();
  const view = open();
  await picker(user);
  await user.click(screen.getByRole("checkbox", { name: "选择 说明.docx" }));
  view.unmount();
  const restored = open();
  await picker(user);
  expect(
    screen.getByRole("checkbox", { name: "选择 说明.docx" }),
  ).toBeChecked();
  await send(user);
  await screen.findByRole("button", { name: "重新连接" });
  restored.unmount();
  open();
  await waitFor(() =>
    expect(
      server.requests.filter((value) => value.path === "/api/chat/stream"),
    ).toHaveLength(2),
  );
  const requests = server.requests.filter(
    (value) => value.path === "/api/chat/stream",
  );
  expect(requests[1].body).toEqual(requests[0].body);
  expect(requests[1].body?.attachment_ids).toEqual(["doc"]);
});

test("leaving during an upload aborts the subscriber and does not attach its result on return", async () => {
  const server = backend({ delayedUpload: true });
  const user = userEvent.setup();
  const view = open();
  await picker(user);
  await user.upload(
    screen.getByLabelText("上传聊天附件"),
    new File(["fixture"], "上传.md"),
  );
  const request = server.requests.find(
    (value) => value.init.method === "POST",
  )!;
  view.unmount();
  expect(request.init.signal?.aborted).toBe(true);
  await server.release();
  open();
  await picker(user);
  expect(
    screen.getByRole("checkbox", { name: "选择 上传.md" }),
  ).not.toBeChecked();
});

test("model capability checkboxes save images and PDF support without changing unrelated profile fields", async () => {
  let submitted: Record<string, unknown> | undefined;
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    expect(path).toBe("/api/models");
    expect(init.method).toBe("POST");
    submitted = JSON.parse(String(init.body));
    return new Response(
      JSON.stringify({ ...model, profile: submitted!.profile }),
    );
  });
  const user = userEvent.setup();
  render(
    <ModelEditor
      api={new LingApi("attachment-owner", () => {})}
      existing={{
        ...model,
        profile: {
          ...model.profile,
          capabilities: {
            ...model.profile.capabilities,
            encrypted_reasoning: true,
          },
        },
      }}
      saved={async () => {}}
    />,
  );
  await user.click(screen.getByText("高级选项：服务支持的能力"));
  await user.click(screen.getByRole("checkbox", { name: "图片输入" }));
  await user.click(screen.getByRole("checkbox", { name: "PDF 输入" }));
  await user.click(screen.getByRole("button", { name: "保存模型配置" }));
  await waitFor(() =>
    expect(submitted).toMatchObject({
      profile_id: "text",
      profile: {
        ...model.profile,
        capabilities: {
          ...model.profile.capabilities,
          encrypted_reasoning: true,
          images: true,
          pdf: true,
        },
      },
    }),
  );
  expect(submitted).not.toHaveProperty("api_key");
  expect(Object.values(sessionStorage).join(" ")).not.toContain(
    "attachment-owner",
  );
});

test("a PDF declaration on a Chat Completions profile cannot enable native scanned-PDF sending", async () => {
  const server = backend();
  const user = userEvent.setup();
  open();
  await picker(user);
  await user.type(screen.getByLabelText("消息"), "读取扫描内容");
  await chooseModel(user, "Chat 接口");
  await user.click(screen.getByRole("checkbox", { name: "选择 扫描.pdf" }));
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(screen.getByText(/当前接口不支持扫描 PDF/)).toBeVisible();
  expect(
    server.requests.some((value) => value.path === "/api/chat/stream"),
  ).toBe(false);
});

test("restored missing attachment metadata can be cleared so a plain-text send remains available", async () => {
  const server = backend();
  sessionStorage.setItem(
    'lingnexus.chat.v1.["ling","conversation"]',
    JSON.stringify({
      turns: [],
      draft: "继续文字交流",
      profileId: null,
      attachmentIds: ["missing"],
    }),
  );
  const user = userEvent.setup();
  open();
  await screen.findByText("原回复");
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "清空附件选择" }));
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  expect(
    server.requests.find((value) => value.path === "/api/chat/stream")!.body,
  ).not.toHaveProperty("attachment_ids");
});

test("older linked files outside the saved-file list resolve names and retain their IDs for regeneration", async () => {
  const server = backend({ oldAttachments: ["doc"], omitDocFromList: true });
  const user = userEvent.setup();
  open();
  expect(
    await within(screen.getByRole("log")).findByText("说明.docx"),
  ).toBeVisible();
  expect(
    server.requests.some((value) => value.path === "/api/attachments/doc"),
  ).toBe(true);
  await user.click(screen.getByRole("button", { name: "重新生成" }));
  expect(
    server.requests.find((value) => value.path === "/api/chat/stream")!.body
      ?.attachment_ids,
  ).toEqual(["doc"]);
});
