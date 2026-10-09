import type {
  Agent,
  AgentPermissions,
  AgentUsage,
  Attachment,
  PermissionUpdate,
  ChatRequest,
  ChatRun,
  Conversation,
  ConversationMessage,
  ConversationBranch,
  EntryInput,
  EntryWrite,
  Episode,
  IdentityBundle,
  IdentityState,
  Instructions,
  PersonaEntry,
  Proposal,
  Revision,
  Source,
  SourceRef,
} from "./types";
import type { ModelInput, ModelSelection, SavedModel } from "./types";

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
  state = (id: string, signal?: AbortSignal) =>
    this.request<IdentityState>(
      "/agents/" + encodeURIComponent(id) + "/state",
      { signal },
    );
  proposals = (id: string, signal?: AbortSignal) =>
    this.request<Proposal[]>(
      "/agents/" + encodeURIComponent(id) + "/proposals",
      { signal },
    );
  revisions = (id: string, signal?: AbortSignal) =>
    this.request<Revision[]>(
      "/agents/" + encodeURIComponent(id) + "/revisions",
      { signal },
    );
  identity = async (
    id: string,
    signal?: AbortSignal,
  ): Promise<IdentityBundle> => {
    const [state, proposals, revisions, entries, instructions] =
      await Promise.all([
        this.state(id, signal),
        this.proposals(id, signal),
        this.revisions(id, signal),
        this.entries(id, signal),
        this.instructions(id, signal),
      ]);
    return { state, proposals, revisions, entries, instructions };
  };
  entries = async (id: string, signal?: AbortSignal) => {
    const payload = await this.request<{
      agent_id: string;
      entries: PersonaEntry[];
    }>("/agents/" + encodeURIComponent(id) + "/entries", { signal });
    return payload.entries;
  };
  instructions = (id: string, signal?: AbortSignal) =>
    this.request<Instructions>(
      "/agents/" + encodeURIComponent(id) + "/instructions",
      { signal },
    );
  saveInstructions = (id: string, rules: string[]) =>
    this.request<Instructions>(
      "/agents/" + encodeURIComponent(id) + "/instructions",
      { method: "PUT", body: JSON.stringify({ rules }) },
    );
  resetInstructions = (id: string) =>
    this.post<Instructions>(
      "/agents/" + encodeURIComponent(id) + "/instructions/reset",
      {},
    );
  addEntry = (id: string, input: EntryInput) =>
    this.post<EntryWrite>(
      "/agents/" + encodeURIComponent(id) + "/entries",
      input,
    );
  removeEntry = (id: string, key: string, expectedRevision: number) =>
    this.post<EntryWrite>(
      "/agents/" + encodeURIComponent(id) + "/entries/remove",
      { key, expected_revision: expectedRevision },
    );
  decide = (
    id: string,
    decision: "approve" | "reject",
    expected_revision: number,
  ) =>
    this.post<Proposal>("/proposals/" + encodeURIComponent(id) + "/decision", {
      decision,
      expected_revision,
    });
  rollback = (id: string, target_revision: number, expected_revision: number) =>
    this.post<IdentityState>(
      "/agents/" + encodeURIComponent(id) + "/rollback",
      { target_revision, expected_revision },
    );
  episodes = (
    id: string,
    query: string,
    offset: number,
    signal?: AbortSignal,
  ) =>
    this.request<Episode[]>(
      "/agents/" +
        encodeURIComponent(id) +
        "/episodes?" +
        new URLSearchParams({ query, limit: "20", offset: String(offset) }),
      { signal },
    );
  episode = (id: string, signal?: AbortSignal) =>
    this.request<Episode>("/episodes/" + encodeURIComponent(id), { signal });
  source = (ref: SourceRef, signal?: AbortSignal) =>
    this.request<Source>(
      "/sources/" + encodeURIComponent(ref.id) + "?revision=" + ref.revision,
      { signal },
    );
  maintain = (operation: "export" | "backup", path: string) =>
    this.post<unknown>("/maintenance/" + operation, { path });
  permissions = (id: string, signal?: AbortSignal) =>
    this.request<AgentPermissions>(
      "/agents/" + encodeURIComponent(id) + "/permissions",
      { signal },
    );
  savePermissions = (
    id: string,
    update: PermissionUpdate,
    signal?: AbortSignal,
  ) =>
    this.request<AgentPermissions>(
      "/agents/" + encodeURIComponent(id) + "/permissions",
      { method: "PUT", body: JSON.stringify(update), signal },
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
  createConversation = (
    agentId: string,
    title: string,
    scene?: string,
    background?: string,
  ) =>
    this.post<Conversation>("/conversations", {
      agent_id: agentId,
      title,
      ...(scene ? { scene, background: background ?? "" } : {}),
    });
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
}
