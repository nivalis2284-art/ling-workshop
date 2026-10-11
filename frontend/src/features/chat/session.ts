import { ApiError, errorMessage, type LingApi } from "../../api/client";
import type {
  ChatEvent,
  ChatRequest,
  ChatRun,
  ChatUsage,
  ConversationMessage,
  ConversationBranch,
} from "../../api/types";
import {
  chatErrorMessage,
  chatRunErrorMessage,
  ChatStreamError,
} from "./errors";
import { readChatStream } from "./stream";

export type TurnStatus =
  | "connecting"
  | "streaming"
  | "disconnected"
  | "complete"
  | "error"
  | "stopped";
export interface ChatTurn {
  request: ChatRequest;
  runId?: string;
  lastSeq: string;
  text: string;
  thinking: string;
  usage?: ChatUsage;
  assistantMessageId?: string | null;
  status: TurnStatus;
  error?: string;
  cancelling?: boolean;
  userMessageId?: string;
  switchedProfile?: string;
  notice?: string;
}
export interface ChatSnapshot {
  turns: ChatTurn[];
  draft: string;
  profileId: string | null;
  effort: string;
  attachmentIds: string[];
  conversationId?: string;
  messages: ConversationMessage[];
  branches: ConversationBranch[];
  historyStatus: "idle" | "loading" | "ready" | "error";
  historyError?: string;
  busy: boolean;
  actionError?: string;
}
const storagePrefix = "lingnexus.chat.v1.";
export const isPending = (turn?: ChatTurn) =>
  !!turn && ["connecting", "streaming", "disconnected"].includes(turn.status);

export function clearChatSessions() {
  try {
    for (const key of Object.keys(sessionStorage))
      if (key.startsWith(storagePrefix)) sessionStorage.removeItem(key);
  } catch {
    /* Storage can be disabled by the browser. */
  }
}

function storageKey(agentId: string, conversationId?: string) {
  return (
    storagePrefix +
    (conversationId ? JSON.stringify([agentId, conversationId]) : agentId)
  );
}

function restore(agentId: string, conversationId?: string): ChatSnapshot {
  const empty: ChatSnapshot = {
    turns: [],
    draft: "",
    profileId: null,
    effort: "",
    attachmentIds: [],
    conversationId,
    messages: [],
    branches: [],
    historyStatus: conversationId ? "idle" : "ready",
    busy: false,
  };
  try {
    const saved = JSON.parse(
      sessionStorage.getItem(storageKey(agentId, conversationId)) ?? "null",
    );
    if (
      !saved ||
      !Array.isArray(saved.turns) ||
      typeof saved.draft !== "string" ||
      (saved.profileId !== null && typeof saved.profileId !== "string")
    )
      return empty;
    // Only accept snapshots from this feature, never treat arbitrary storage as history.
    if (
      !saved.turns.every(
        (turn: ChatTurn) =>
          turn &&
          turn.request?.agent_id === agentId &&
          (turn.request.conversation_id === undefined ||
            turn.request.conversation_id ===
              (conversationId ?? saved.conversationId)) &&
          typeof turn.request.message === "string" &&
          typeof turn.request.request_id === "string" &&
          (turn.request.attachment_ids === undefined ||
            (Array.isArray(turn.request.attachment_ids) &&
              turn.request.attachment_ids.every(
                (id) => typeof id === "string" && id.length > 0,
              ))) &&
          (turn.runId === undefined || typeof turn.runId === "string") &&
          typeof turn.lastSeq === "string" &&
          /^\d+$/.test(turn.lastSeq) &&
          typeof turn.text === "string" &&
          typeof turn.thinking === "string" &&
          [
            "connecting",
            "streaming",
            "disconnected",
            "complete",
            "error",
            "stopped",
          ].includes(turn.status),
      )
    )
      return empty;
    return {
      ...empty,
      draft: saved.draft,
      profileId: saved.profileId,
      effort: typeof saved.effort === "string" ? saved.effort : "",
      attachmentIds:
        Array.isArray(saved.attachmentIds) &&
        saved.attachmentIds.every(
          (id: unknown) => typeof id === "string" && id.length > 0,
        )
          ? saved.attachmentIds
          : [],
      conversationId:
        conversationId ??
        (typeof saved.conversationId === "string"
          ? saved.conversationId
          : undefined),
      // Browser data restores subscribers and drafts, never completed history.
      turns: saved.turns.filter(isPending).map((turn: ChatTurn) => ({
        ...turn,
        cancelling: false,
        status: isPending(turn) ? "disconnected" : turn.status,
      })),
    };
  } catch {
    return empty;
  }
}

// One store per conversation; its lifetime is independent of the open page.
export class ChatSession {
  private state: ChatSnapshot;
  private listeners = new Set<() => void>();
  private controller: AbortController | null = null;
  private attached = false;
  private historyController: AbortController | null = null;
  private runController: AbortController | null = null;
  private conversations = new Map<string, ChatSession>();

  constructor(
    private api: LingApi,
    private agentId: string,
    private storageConversationId?: string,
  ) {
    this.state = restore(agentId, storageConversationId);
  }
  forConversation = (conversationId: string): ChatSession => {
    if (this.state.conversationId === conversationId) return this;
    let session = this.conversations.get(conversationId);
    if (!session) {
      session = new ChatSession(this.api, this.agentId, conversationId);
      this.conversations.set(conversationId, session);
    }
    return session;
  };
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(patch: Partial<ChatSnapshot>) {
    this.state = { ...this.state, ...patch };
    try {
      // Explicit allowlist: no owner token, provider configuration or model key.
      sessionStorage.setItem(
        storageKey(this.agentId, this.storageConversationId),
        JSON.stringify({
          turns: this.state.turns.filter(isPending),
          draft: this.state.draft,
          profileId: this.state.profileId,
          effort: this.state.effort,
          attachmentIds: this.state.attachmentIds,
          conversationId: this.state.conversationId,
        }),
      );
      if (this.state.conversationId && !this.storageConversationId)
        sessionStorage.setItem(
          storageKey(this.agentId, this.state.conversationId),
          sessionStorage.getItem(storageKey(this.agentId))!,
        );
    } catch {
      /* In-memory chat remains usable when storage is unavailable. */
    }
    this.listeners.forEach((listener) => listener());
  }
  private change(id: string, patch: Partial<ChatTurn>) {
    this.update({
      turns: this.state.turns.map((turn) =>
        turn.request.request_id === id ? { ...turn, ...patch } : turn,
      ),
    });
  }
  setDraft = (draft: string) => this.update({ draft });
  setProfile = (profileId: string) => this.update({ profileId, effort: "" });
  setEffort = (effort: string) => this.update({ effort });
  setAttachments = (attachmentIds: string[]) =>
    this.update({ attachmentIds: [...new Set(attachmentIds)] });
  refreshHistory = async (): Promise<boolean> => {
    const cid = this.state.conversationId;
    if (!cid) return true;
    this.historyController?.abort();
    const controller = new AbortController();
    this.historyController = controller;
    this.update({ historyStatus: "loading", historyError: undefined });
    try {
      const turn = this.state.turns.at(-1);
      const [messages, branches, run] = await Promise.all([
        this.api.conversationMessages(cid, controller.signal),
        this.api.conversationBranches(cid, controller.signal),
        // started only guarantees conversation_id. Match a terminal or
        // restored attempt to its persisted user node using the run record.
        turn?.runId && !turn.userMessageId
          ? this.api
              .chatRun(turn.runId, controller.signal)
              .catch(() => undefined)
          : Promise.resolve(undefined),
      ]);
      if (controller.signal.aborted || this.historyController !== controller)
        return false;
      if (run && turn?.runId) {
        this.checkRun(run, turn.runId);
        this.adoptRun(run);
      }
      this.update({ messages, branches, historyStatus: "ready" });
      // A terminal run resolved through GET status carries no assistant id;
      // adopt the selected branch's answer so the round stays linked to it.
      const latest = this.state.turns.at(-1);
      if (latest && latest.status === "complete" && !latest.assistantMessageId) {
        const answer = branches.find(
          (branch) => branch.selected && branch.role === "assistant",
        );
        if (answer)
          this.change(latest.request.request_id, {
            assistantMessageId: answer.id,
          });
      }
      return true;
    } catch (error) {
      if (!controller.signal.aborted && this.historyController === controller)
        this.update({
          historyStatus: "error",
          historyError: errorMessage(error),
        });
      return false;
    } finally {
      if (this.historyController === controller) this.historyController = null;
    }
  };
  private canMutate() {
    return (
      !isPending(this.state.turns.at(-1)) &&
      !this.state.busy &&
      this.state.historyStatus === "ready"
    );
  }
  editMessage = async (
    messageId: string,
    content: string,
  ): Promise<boolean> => {
    if (
      !this.canMutate() ||
      !this.state.conversationId ||
      !content.trim() ||
      !this.state.messages.some(
        (message) => message.id === messageId && message.role === "user",
      )
    )
      return false;
    return this.mutateBranch(() =>
      this.api.editConversationMessage(
        this.state.conversationId!,
        messageId,
        content,
        crypto.randomUUID(),
      ),
    );
  };
  deleteTurns = async (anchors: string[]): Promise<boolean> => {
    if (
      !this.canMutate() ||
      !this.state.conversationId ||
      !anchors.length ||
      !anchors.every((id) =>
        this.state.messages.some(
          (message) => message.id === id && message.role === "user",
        ),
      )
    )
      return false;
    return this.mutateBranch(() =>
      this.api.deleteConversationTurns(this.state.conversationId!, anchors),
    );
  };
  selectBranch = async (messageId: string) => {
    if (
      !this.canMutate() ||
      !this.state.conversationId ||
      !this.state.branches.some((branch) => branch.id === messageId)
    )
      return;
    await this.mutateBranch(() =>
      this.api.selectConversationHead(this.state.conversationId!, messageId),
    );
  };
  private async mutateBranch(operation: () => Promise<unknown>) {
    this.update({ busy: true, actionError: undefined });
    try {
      await operation();
      this.update({ turns: [], messages: [], branches: [] });
      return await this.refreshHistory();
    } catch (error) {
      this.update({ actionError: errorMessage(error) });
      return false;
    } finally {
      this.update({ busy: false });
    }
  }
  attach = () => {
    this.attached = true;
    void this.resume();
    return this.detach;
  };
  private async resume() {
    const turn = this.state.turns.at(-1);
    // A replay may start with text instead of started. Resolve membership from
    // its durable run before the page chooses a different conversation store.
    if (isPending(turn) && turn?.runId && !turn.userMessageId) {
      const controller = new AbortController();
      this.runController = controller;
      try {
        const run = await this.api.chatRun(turn.runId, controller.signal);
        if (controller.signal.aborted || this.runController !== controller)
          return;
        this.checkRun(run, turn.runId);
        this.adoptRun(run);
      } catch {
        if (controller.signal.aborted || this.runController !== controller)
          return;
        // Replaying remains available when status inspection is unavailable.
      } finally {
        if (this.runController === controller) this.runController = null;
      }
    }
    if (!this.attached) return;
    void this.refreshHistory();
    if (isPending(this.state.turns.at(-1))) void this.reconnect();
  }
  detach = () => {
    this.attached = false;
    this.controller?.abort();
    this.controller = null;
    this.historyController?.abort();
    this.historyController = null;
    this.runController?.abort();
    this.runController = null;
    const turn = this.state.turns.at(-1);
    if (isPending(turn))
      this.change(turn!.request.request_id, { status: "disconnected" });
  };
  send = (message: string, profileId: string, effort?: string) => {
    this.submit(
      message.trim(),
      profileId,
      effort,
      undefined,
      this.state.attachmentIds,
    );
  };
  regenerate = (assistantId: string, profileId: string, effort?: string) => {
    const assistant = this.state.messages.find(
      (message) => message.id === assistantId && message.role === "assistant",
    );
    const user = this.state.messages.find(
      (message) =>
        message.id === assistant?.parent_id && message.role === "user",
    );
    if (!user || !this.state.conversationId) return;
    this.submit(
      user.text,
      profileId,
      effort,
      user.id,
      user.attachment_ids ?? [],
    );
  };
  retry = (profileId: string, effort?: string) => {
    const last = this.state.turns.at(-1);
    if (last?.status === "error")
      this.submit(
        last.request.message,
        profileId,
        effort,
        last.request.regenerate_message_id,
        last.request.attachment_ids ?? [],
      );
  };
  private submit(
    message: string,
    profileId: string,
    effort?: string,
    regenerateMessageId?: string,
    attachmentIds: string[] = [],
  ) {
    if (!message.trim() || !profileId || !this.canMutate() || this.controller)
      return;
    const request: ChatRequest = {
      agent_id: this.agentId,
      message,
      profile_id: profileId,
      request_id: crypto.randomUUID(),
      ...(this.state.conversationId
        ? { conversation_id: this.state.conversationId }
        : {}),
      ...(effort ? { effort } : {}),
      ...(regenerateMessageId
        ? { regenerate_message_id: regenerateMessageId }
        : {}),
      ...(attachmentIds.length ? { attachment_ids: [...attachmentIds] } : {}),
    };
    const turn: ChatTurn = {
      request,
      text: "",
      thinking: "",
      lastSeq: "0",
      status: "connecting",
    };
    this.update({
      turns: [...this.state.turns, turn],
      ...(regenerateMessageId ? {} : { draft: "", attachmentIds: [] }),
      actionError: undefined,
    });
    void this.connect(turn);
  }
  reconnect = async () => {
    const turn = this.state.turns.at(-1);
    if (isPending(turn) && !this.controller) await this.connect(turn!);
  };
  private async connect(turn: ChatTurn) {
    if (!this.attached || this.controller) return;
    const id = turn.request.request_id;
    const controller = new AbortController();
    this.controller = controller;
    this.change(id, { status: "connecting", error: undefined });
    try {
      const response = turn.runId
        ? await this.api.chatEvents(turn.runId, turn.lastSeq, controller.signal)
        : await this.api.startChat(turn.request, controller.signal);
      await readChatStream(
        response,
        (event) => {
          if (!controller.signal.aborted && this.controller === controller)
            this.receive(id, event);
        },
        controller.signal,
      );
    } catch (error) {
      if (!controller.signal.aborted && this.controller === controller) {
        const current = this.state.turns.find(
          (item) => item.request.request_id === id,
        )!;
        // An empty replay can be legitimate if the saved cursor is already at
        // the terminal event. A stream with new data still needs its final event.
        if (
          error instanceof ChatStreamError &&
          error.code === "unexpected_eof" &&
          current.runId &&
          current.lastSeq === turn.lastSeq
        ) {
          try {
            const run = await this.api.chatRun(
              current.runId,
              controller.signal,
            );
            if (controller.signal.aborted || this.controller !== controller)
              return;
            this.checkRun(run, current.runId);
            if (run.status !== "running") {
              this.adoptRun(run);
              this.change(id, this.runResult(run));
              void this.refreshHistory();
              return;
            }
          } catch (statusError) {
            if (controller.signal.aborted || this.controller !== controller)
              return;
            error = statusError;
          }
        }
        const rejected =
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500;
        this.change(id, {
          status: rejected ? "error" : "disconnected",
          error: chatErrorMessage(error),
        });
      }
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }
  private receive(id: string, event: ChatEvent) {
    const turn = this.state.turns.find(
      (item) => item.request.request_id === id,
    )!;
    if (turn.runId && turn.runId !== event.data.run_id)
      throw new ChatStreamError("malformed_stream");
    if (BigInt(event.id) <= BigInt(turn.lastSeq)) return;
    // Every runtime event identifies its run, including a resumed stream that
    // starts with text/usage or a terminal event instead of started.
    const patch: Partial<ChatTurn> = {
      lastSeq: event.id,
      runId: event.data.run_id,
    };
    switch (event.event) {
      case "started":
        if (event.data.conversation_id) {
          if (
            this.state.conversationId &&
            this.state.conversationId !== event.data.conversation_id
          )
            throw new ChatStreamError("malformed_stream");
          this.update({ conversationId: event.data.conversation_id });
        }
        if (event.data.user_message_id)
          patch.userMessageId = event.data.user_message_id;
        patch.status = "streaming";
        break;
      case "model_switch":
        patch.switchedProfile = event.data.to_profile;
        patch.status = "streaming";
        if (event.data.restart) {
          patch.text = "";
          patch.thinking = "";
          patch.usage = undefined;
          patch.assistantMessageId = undefined;
        }
        break;
      case "notice":
        if (event.data.code === "effort_omitted")
          patch.notice = "备用模型不支持所选思考强度，已使用模型默认设置。";
        else if (event.data.code === "context_truncated")
          patch.notice =
            event.data.message ?? "对话超出模型窗口，已丢弃最早的部分消息。";
        break;
      case "text":
        patch.text = turn.text + event.data.text;
        patch.status = "streaming";
        break;
      case "thinking":
        patch.thinking = turn.thinking + event.data.text;
        patch.status = "streaming";
        break;
      case "usage":
        patch.usage = event.data.usage;
        break;
      case "complete":
        patch.status = "complete";
        patch.assistantMessageId = event.data.assistant_message_id;
        patch.error = undefined;
        break;
      case "error":
        patch.status = event.data.code === "cancelled" ? "stopped" : "error";
        patch.error =
          event.data.code === "cancelled"
            ? undefined
            : chatRunErrorMessage(event.data.code);
        break;
    }
    this.change(id, patch);
    if (event.event === "complete" || event.event === "error")
      void this.refreshHistory();
  }
  private checkRun(run: ChatRun, runId: string) {
    if (
      run.id !== runId ||
      run.agent_id !== this.agentId ||
      (run.conversation_id &&
        this.state.conversationId &&
        run.conversation_id !== this.state.conversationId) ||
      !["running", "completed", "cancelled", "failed", "interrupted"].includes(
        run.status,
      )
    )
      throw new ChatStreamError("malformed_stream");
  }
  private adoptRun(run: ChatRun) {
    if (run.conversation_id)
      this.update({ conversationId: run.conversation_id });
    if (run.user_message_id)
      this.change(run.request_id, { userMessageId: run.user_message_id });
  }
  private runResult(run: ChatRun): Partial<ChatTurn> {
    return {
      status:
        run.status === "completed"
          ? "complete"
          : run.status === "cancelled"
            ? "stopped"
            : "error",
      error: ["completed", "cancelled"].includes(run.status)
        ? undefined
        : chatRunErrorMessage(run.status),
    };
  }
  stop = async () => {
    const turn = this.state.turns.at(-1);
    if (!turn?.runId || !isPending(turn) || turn.cancelling) return;
    const id = turn.request.request_id;
    this.change(id, { cancelling: true, error: undefined });
    try {
      const run = await this.api.cancelChat(turn.runId);
      this.checkRun(run, turn.runId);
      this.adoptRun(run);
      if (run.status === "running") throw new ChatStreamError("cancel_failed");
      const current = this.state.turns.find(
        (item) => item.request.request_id === id,
      );
      if (isPending(current)) {
        this.controller?.abort();
        this.controller = null;
        if (run.status === "cancelled") {
          this.change(id, { status: "stopped", error: undefined });
          void this.refreshHistory();
        } else {
          // Completion won the race with Stop. Replay the tail so the final
          // text and terminal state are retained instead of claiming a stop.
          this.change(id, { status: "disconnected", error: undefined });
          if (this.attached) void this.reconnect();
        }
      }
    } catch {
      this.change(id, {
        error:
          "停止请求失败，生成可能仍在继续。请重试停止，或重新连接查看进度。",
      });
    } finally {
      this.change(id, { cancelling: false });
    }
  };
}

export class ChatSessions {
  private sessions = new Map<string, ChatSession>();
  constructor(private api: LingApi) {}
  forAgent(agentId: string, conversationId?: string) {
    let session = this.sessions.get(agentId);
    if (!session) {
      session = new ChatSession(this.api, agentId);
      this.sessions.set(agentId, session);
    }
    return conversationId ? session.forConversation(conversationId) : session;
  }
}
