import type {
  Agent,
  AgentInstructions,
  AgentPermissions,
  AgentUsage,
  Attachment,
  PermissionUpdate,
  ChatRequest,
  ChatRun,
  Conversation,
  ConversationAccess,
  ConversationMessage,
  ConversationBranch,
  ModelInput,
  ModelSelection,
  McpServerEntry,
  PendingConfirmation,
  SandboxLevel,
  SandboxStatus,
  SavedModel,
  SkillpackView,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly code?: string,
  ) {
    super(message);
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}

export class LingApi {
  constructor(
    private readonly token: string,
    private readonly onUnauthorized: () => void,
  ) {}

  private async response(
    path: string,
    options: RequestInit = {},
    accept = "application/json",
  ): Promise<Response> {
    let response: Response;
    try {
      response = await fetch("/api" + path, {
        ...options,
        credentials: "omit",
        cache: "no-store",
        headers: {
          Accept: accept,
          Authorization: "Bearer " + this.token,
          ...(typeof options.body === "string"
            ? { "Content-Type": "application/json" }
            : {}),
        },
      });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new ApiError(
        0,
        "暂时连接不到本地服务。请确认 LingNexus 正在运行，再重试。",
      );
    }
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "本机会话已失效，请从启动器重新打开。");
    }
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? payload.detail
          : null;
      const message =
        typeof detail === "string" && detail.length < 500
          ? detail.split(this.token).join("[会话信息已隐藏]")
          : response.status === 403
            ? "本地服务拒绝了访问，请从启动器重新打开。"
            : response.status >= 500
              ? "本地服务暂时无法完成请求，请稍后重试。"
              : "请求未完成，请检查输入后重试。";
      const code =
        typeof detail === "string"
          ? detail
          : detail &&
              typeof detail === "object" &&
              "code" in detail &&
              typeof detail.code === "string"
            ? detail.code
            : payload &&
                typeof payload === "object" &&
                "code" in payload &&
                typeof payload.code === "string"
              ? payload.code
              : undefined;
      throw new ApiError(response.status, message, code);
    }
    return response;
  }

  private async request<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const response = await this.response(path, options);
    const payload: unknown = await response.json().catch(() => null);
    if (payload === null)
      throw new ApiError(0, "本地服务返回了无法读取的数据，请重试。");
    return payload as T;
  }

  private post<T>(path: string, body: unknown) {
    return this.request<T>(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  private validateLocalPath(path: string) {
    if (!path.startsWith("/") || path.startsWith("//"))
      throw new ApiError(0, "接口路径必须以 / 开头，并使用本地服务路径。");
  }

  async call<T>(path: string, options: RequestInit = {}): Promise<T> {
    this.validateLocalPath(path);
    return this.request<T>(path, options);
  }

  async binary(path: string, signal?: AbortSignal): Promise<Blob> {
    this.validateLocalPath(path);
    const response = await this.response(
      path,
      { signal },
      "application/octet-stream",
    );
    return response.blob();
  }

  agents = (signal?: AbortSignal) =>
    this.request<Agent[]>("/agents", { signal });
  createAgent = (name: string) =>
    this.post<{ id: string }>("/agents", { name });
  maintain = (operation: "export" | "backup", path: string) =>
    this.post<unknown>("/maintenance/" + operation, { path });
  sandbox = (signal?: AbortSignal) =>
    this.request<SandboxStatus>("/sandbox", { signal });
  setSandboxLevel = (level: SandboxLevel, signal?: AbortSignal) =>
    this.request<SandboxStatus>("/sandbox", {
      method: "PUT",
      body: JSON.stringify({ level }),
      signal,
    });
  permissions = (id: string, signal?: AbortSignal) =>
    this.request<AgentPermissions>(
      "/agents/" + encodeURIComponent(id) + "/permissions",
      { signal },
    );  savePermissions = (
    id: string,
    update: PermissionUpdate,
    signal?: AbortSignal,
  ) =>
    this.request<AgentPermissions>(
      "/agents/" + encodeURIComponent(id) + "/permissions",
      { method: "PUT", body: JSON.stringify(update), signal },
    );
  instructions = (id: string, signal?: AbortSignal) =>
    this.request<AgentInstructions>(
      "/agents/" + encodeURIComponent(id) + "/instructions",
      { signal },
    );
  saveInstructions = (id: string, rules: string[], signal?: AbortSignal) =>
    this.request<AgentInstructions>(
      "/agents/" + encodeURIComponent(id) + "/instructions",
      { method: "PUT", body: JSON.stringify({ rules }), signal },
    );
  resetInstructions = (id: string, signal?: AbortSignal) =>
    this.request<AgentInstructions>(
      "/agents/" + encodeURIComponent(id) + "/instructions/reset",
      { method: "POST", signal },
    );
  usage = (id: string, signal?: AbortSignal) =>
    this.request<AgentUsage>("/agents/" + encodeURIComponent(id) + "/usage", {
      signal,
    });
  attachments = (id: string, signal?: AbortSignal) =>
    this.request<Attachment[]>(
      "/agents/" + encodeURIComponent(id) + "/attachments",
      { signal },
    );
  uploadAttachment = (id: string, file: File, signal?: AbortSignal) => {
    const body = new FormData();
    body.append("file", file);
    return this.request<Attachment>(
      "/agents/" + encodeURIComponent(id) + "/attachments",
      { method: "POST", body, signal },
    );
  };
  attachment = (id: string, signal?: AbortSignal) =>
    this.request<Attachment>("/attachments/" + encodeURIComponent(id), {
      signal,
    });
  downloadAttachment = async (
    id: string,
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const response = await this.response(
      "/attachments/" + encodeURIComponent(id) + "/download",
      { signal },
      "application/octet-stream",
    );
    return response.blob();
  };
  models = async (signal?: AbortSignal) => {
    const models = await this.request<SavedModel[]>("/models", { signal });
    // Older profiles may omit the optional wire capability. Consumers receive
    // an empty list so settings and chat agree that effort is unsupported.
    return models.map((model) => ({
      ...model,
      profile: {
        ...model.profile,
        capabilities: {
          ...model.profile.capabilities,
          effort_levels: model.profile.capabilities.effort_levels ?? [],
        },
      },
    }));
  };
  modelSelection = (signal?: AbortSignal) =>
    this.request<ModelSelection>("/models/selection", { signal });
  saveModel = (input: ModelInput) => this.post<SavedModel>("/models", input);
  selectModels = (primary: string, fallback: string | null) =>
    this.request<ModelSelection>("/models/selection", {
      method: "PUT",
      body: JSON.stringify({ primary, fallback }),
    });
  startChat = (request: ChatRequest, signal: AbortSignal) =>
    this.response(
      "/chat/stream",
      {
        method: "POST",
        body: JSON.stringify(request),
        signal,
      },
      "text/event-stream",
    );
  conversations = (agentId: string, signal?: AbortSignal) =>
    this.request<Conversation[]>(
      "/agents/" + encodeURIComponent(agentId) + "/conversations",
      { signal },
    );
  conversationMessages = (id: string, signal?: AbortSignal) =>
    this.request<ConversationMessage[]>(
      "/conversations/" + encodeURIComponent(id) + "/messages",
      { signal },
    );
  conversationBranches = (id: string, signal?: AbortSignal) =>
    this.request<ConversationBranch[]>(
      "/conversations/" + encodeURIComponent(id) + "/branches",
      { signal },
    );
  editConversationMessage = (
    id: string,
    messageId: string,
    content: string,
    requestId: string,
  ) =>
    this.post<ConversationMessage>(
      "/conversations/" + encodeURIComponent(id) + "/edit",
      { message_id: messageId, content, request_id: requestId },
    );
  selectConversationHead = (id: string, messageId: string) =>
    this.post<Conversation>(
      "/conversations/" + encodeURIComponent(id) + "/head",
      { message_id: messageId },
    );
  deleteConversationTurns = (id: string, anchors: string[]) =>
    this.post<Conversation>(
      "/conversations/" + encodeURIComponent(id) + "/delete-turns",
      { anchors },
    );
  conversationAccess = (id: string, signal?: AbortSignal) =>
    this.request<ConversationAccess>(
      "/conversations/" + encodeURIComponent(id) + "/access",
      { signal },
    );
  setConversationAccess = (id: string, mode: ConversationAccess["mode"]) =>
    this.post<ConversationAccess>(
      "/conversations/" + encodeURIComponent(id) + "/access",
      { mode },
    );
  mcpServers = (signal?: AbortSignal) =>
    this.request<{ servers: McpServerEntry[] }>("/mcp/servers", { signal });
  saveMcpServers = (servers: McpServerEntry[]) =>
    this.request<{ servers: McpServerEntry[] }>("/mcp/servers", {
      method: "PUT",
      body: JSON.stringify({ servers }),
    });
  skillpacks = (signal?: AbortSignal) =>
    this.request<SkillpackView>("/skillpacks", { signal });
  chatEvents = (runId: string, after: string, signal: AbortSignal) =>
    this.response(
      "/chat/runs/" +
        encodeURIComponent(runId) +
        "/events?" +
        new URLSearchParams({ after }),
      { signal },
      "text/event-stream",
    );
  chatRun = (runId: string, signal?: AbortSignal) =>
    this.request<ChatRun>("/chat/runs/" + encodeURIComponent(runId), {
      signal,
    });
  cancelChat = (runId: string) =>
    this.request<ChatRun>(
      "/chat/runs/" + encodeURIComponent(runId) + "/cancel",
      {
        method: "POST",
      },
    );
  pauseChat = (runId: string) =>
    this.request<{ run_id: string; paused: boolean }>(
      "/chat/runs/" + encodeURIComponent(runId) + "/pause",
      { method: "POST" },
    );
  resumeChat = (runId: string) =>
    this.request<{ run_id: string; paused: boolean }>(
      "/chat/runs/" + encodeURIComponent(runId) + "/resume",
      { method: "POST" },
    );
  guideChat = (runId: string, text: string) =>
    this.post<{ run_id: string; queued: number }>(
      "/chat/runs/" + encodeURIComponent(runId) + "/guide",
      { text },
    );
  chatConfirmations = (runId: string, signal?: AbortSignal) =>
    this.request<PendingConfirmation[]>(
      "/chat/runs/" + encodeURIComponent(runId) + "/confirmations",
      { signal },
    );
  decideChatConfirmation = (
    runId: string,
    fingerprint: string,
    approved: boolean,
  ) =>
    this.post<{ run_id: string; fingerprint: string; approved: boolean }>(
      "/chat/runs/" +
        encodeURIComponent(runId) +
        "/confirmations/" +
        encodeURIComponent(fingerprint),
      { approved },
    );
}
