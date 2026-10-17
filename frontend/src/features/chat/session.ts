import { ApiError, errorMessage, type LingApi } from "../../api/client";
import type {
  ChatEvent,
  ChatRequest,
  ChatRun,
  ChatToolCall,
  ChatUsage,
  ConversationMessage,
  ConversationBranch,
  PendingConfirmation,
  VerificationEvidence,
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
  // Live loop state. Persisted for subscribers only; a reload re-syncs it.
  step?: number;
  tools?: ChatToolCall[];
  paused?: boolean;
  // “暂停请求中”是独立状态：后端只在步边界真正暂停并补发 paused 事件，
  // POST /pause 仅置标志，所以不能乐观翻 paused；恢复同理。
  pausePending?: boolean;
  resumePending?: boolean;
  confirmations?: PendingConfirmation[];
  verification?: VerificationEvidence;
  recoverable?: boolean;
  recoveryReason?: string;
  recovering?: boolean;
  // 本回合已提交决策的指纹：决策与回读/通知并发时，靠它防止已处理的卡复活。
  decidedFingerprints?: string[];
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
// A stream can emit many small deltas per second. Persist one coalesced copy
// after the burst so a reload still sees the latest pending turn, without
// serializing the growing transcript on every token.
const streamStorageDelay = 400;
// One page-level listener covers every attached conversation. Detached stores
// stay available for switching back, but neither their timer nor this listener
// holds them alive.
const attachedSessions = new Set<ChatSession>();
const testSessions = new Set<ChatSession>();
let pagehideInstalled = false;
function persistAttachedSessions() {
  for (const session of attachedSessions)
    session.flushPersistenceForLifecycle();
}
function installPagehideListener() {
  if (pagehideInstalled || typeof window === "undefined") return;
  pagehideInstalled = true;
  window.addEventListener("pagehide", persistAttachedSessions);
}
export function resetChatSessionLifecycleForTests() {
  if (import.meta.env.MODE !== "test") return;
  attachedSessions.clear();
  for (const session of testSessions) session.dispose();
  testSessions.clear();
  if (pagehideInstalled && typeof window !== "undefined")
    window.removeEventListener("pagehide", persistAttachedSessions);
  pagehideInstalled = false;
}
export const isPending = (turn?: ChatTurn) =>
  !!turn && ["connecting", "streaming", "disconnected"].includes(turn.status);
const shouldPersist = (turn: ChatTurn) =>
  isPending(turn) ||
  (!!turn.runId &&
    turn.recoverable === true &&
    ["error", "stopped"].includes(turn.status));

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
      turns: saved.turns.filter(shouldPersist).map((turn: ChatTurn) => ({
        ...turn,
        cancelling: false,
        // 在途控制标记不跨页面生命周期：重载后没有请求可等。已决策记账也不
        // 保留——重载可能截断了决策请求，此时必须允许用户重新作答。
        pausePending: false,
        resumePending: false,
        decidedFingerprints: undefined,
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
  private confirmationsController: AbortController | null = null;
  private recoveryRequest: string | null = null;
  private conversations = new Map<string, ChatSession>();
  private storageTimer: ReturnType<typeof setTimeout> | null = null;
  private streamStorageDirty = false;
  private disposed = false;

  constructor(
    private api: LingApi,
    private agentId: string,
    private storageConversationId?: string,
  ) {
    this.state = restore(agentId, storageConversationId);
    if (import.meta.env.MODE === "test") testSessions.add(this);
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
  private update(patch: Partial<ChatSnapshot>, deferPersistence = false) {
    if (this.disposed) return;
    this.state = { ...this.state, ...patch };
    if (deferPersistence) this.schedulePersistence();
    else this.persistNow();
    this.listeners.forEach((listener) => listener());
  }
  private schedulePersistence() {
    this.streamStorageDirty = true;
    if (this.storageTimer !== null) return;
    this.storageTimer = setTimeout(() => {
      this.storageTimer = null;
      if (!this.streamStorageDirty) return;
      this.streamStorageDirty = false;
      this.persistNow();
    }, streamStorageDelay);
  }
  private persistNow = () => {
    if (this.storageTimer !== null) {
      clearTimeout(this.storageTimer);
      this.storageTimer = null;
    }
    this.streamStorageDirty = false;
    try {
      // Explicit allowlist: no owner token, provider configuration or model key.
      sessionStorage.setItem(
        storageKey(this.agentId, this.storageConversationId),
        JSON.stringify({
          turns: this.state.turns.filter(shouldPersist),
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
  };
  flushPersistenceForLifecycle = () => this.persistNow();
  private change(
    id: string,
    patch: Partial<ChatTurn>,
    deferPersistence = false,
  ) {
    this.update({
      turns: this.state.turns.map((turn) =>
        turn.request.request_id === id ? { ...turn, ...patch } : turn,
      ),
    }, deferPersistence);
  }
  setDraft = (draft: string) => this.update({ draft });
  setProfile = (profileId: string) => this.update({ profileId, effort: "" });
  setEffort = (effort: string) => this.update({ effort });
  setAttachments = (attachmentIds: string[]) =>
    this.update({ attachmentIds: [...new Set(attachmentIds)] });
  /** Join a server-started work run without POSTing a second user request. */
  followRun = async (runId: string, signal: AbortSignal): Promise<void> => {
    if (this.state.turns.some((turn) => turn.runId === runId)) return;
    try {
      const run = await this.api.chatRun(runId, signal);
      if (signal.aborted || !this.attached) return;
      this.checkRun(run, runId);
      if (isPending(this.state.turns.at(-1))) return;
      if (this.state.turns.some((turn) => turn.runId === runId)) return;
      const user = this.state.messages.find((message) => message.id === run.user_message_id);
      const turn: ChatTurn = {
        request: {
          agent_id: this.agentId,
          conversation_id: run.conversation_id ?? this.state.conversationId,
          request_id: run.request_id,
          profile_id: run.profile_id,
          message: user?.text ?? "",
        },
        runId,
        userMessageId: run.user_message_id ?? undefined,
        text: "",
        thinking: "",
        lastSeq: "0",
        status: run.status === "running" ? "disconnected" : "complete",
        ...(run.status === "running" ? {} : this.runResult(run)),
      };
      this.update({ turns: [...this.state.turns, turn] });
      if (run.status === "running") {
        void this.syncConfirmations(runId);
        void this.reconnect();
      }
    } catch (error) {
      if (!signal.aborted) this.update({ actionError: errorMessage(error) });
    }
  };
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
      if (
        latest &&
        latest.status === "complete" &&
        !latest.assistantMessageId
      ) {
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
    this.disposed = false;
    installPagehideListener();
    attachedSessions.add(this);
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
    const active = this.state.turns.at(-1);
    // A confirmation issued while this page was away is not replayed by the
    // stream, so re-read the run's pending decisions on reattach.
    if (isPending(active) && active?.runId)
      void this.syncConfirmations(active.runId);
    if (isPending(this.state.turns.at(-1))) void this.reconnect();
  }
  private async syncConfirmations(runId: string) {
    // 新回读发出时旧回读即作废：切换/重连后先发后至的旧快照会把更新的事实
    // （例如刚决策完的项）重新写回。
    this.confirmationsController?.abort();
    const controller = new AbortController();
    this.confirmationsController = controller;
    try {
      const list = await this.api.chatConfirmations(runId, controller.signal);
      if (
        controller.signal.aborted ||
        this.confirmationsController !== controller
      )
        return;
      const turn = this.state.turns.at(-1);
      if (turn?.runId !== runId) return;
      const decided = new Set(turn.decidedFingerprints ?? []);
      // 按 fingerprint 合并而不是整表覆盖：决策与回读并发时任何一方都不该
      // 抹掉对方；已决策的指纹不因旧快照复活，新指纹仍按服务端真源补入。
      const merged = [...(turn.confirmations ?? [])];
      for (const item of list) {
        if (decided.has(item.fingerprint)) continue;
        const index = merged.findIndex(
          (existing) => existing.fingerprint === item.fingerprint,
        );
        if (index >= 0) merged[index] = item;
        else merged.push(item);
      }
      this.change(turn.request.request_id, { confirmations: merged });
    } catch {
      /* 取消或失败都交给流的再次通知；已决策项不会因此复活。 */
    }
  }
  detach = () => {
    attachedSessions.delete(this);
    this.attached = false;
    this.controller?.abort();
    this.controller = null;
    this.historyController?.abort();
    this.historyController = null;
    this.runController?.abort();
    this.runController = null;
    this.confirmationsController?.abort();
    this.confirmationsController = null;
    if (this.storageTimer !== null) {
      clearTimeout(this.storageTimer);
      this.storageTimer = null;
    }
    this.persistNow();
    const turn = this.state.turns.at(-1);
    if (isPending(turn))
      this.change(turn!.request.request_id, { status: "disconnected" });
  };
  /** Release resources without changing the stable conversation store maps. */
  suspend = () => {
    if (this.disposed) return;
    this.detach();
    for (const session of this.conversations.values()) session.suspend();
  };
  dispose = () => {
    if (this.disposed) return;
    this.suspend();
    this.disposed = true;
    for (const session of this.conversations.values()) session.dispose();
    testSessions.delete(this);
    // Keep child identities: React StrictMode replays effect cleanup without
    // rebuilding ChatPage's captured session. The whole owner is GC-eligible
    // once the app re-renders with a new ChatSessions.
    this.persistNow();
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
  get lifecycle() {
    return {
      attached: this.attached,
      pendingStorageWrite: this.storageTimer !== null,
    };
  }
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
  refreshRecovery = async (): Promise<void> => {
    const runId = this.state.turns.at(-1)?.runId;
    if (runId) await this.syncRunState(runId);
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
        if (event.data.code === "runtime_diagnostic")
          patch.notice = event.data.message ?? "运行失败，诊断信息已记录。";
        else if (event.data.code === "effort_omitted")
          patch.notice = "备用模型不支持所选思考强度，已使用模型默认设置。";
        else if (event.data.code === "context_truncated")
          patch.notice =
            event.data.message ?? "对话超出模型窗口，已丢弃最早的部分消息。";
        else if (event.data.code === "step_budget_exhausted")
          patch.notice = event.data.message ?? "已达到步数上限，任务尚未完成。";
        else if (
          event.data.code === "confirmation_required" &&
          event.data.fingerprint
        ) {
          const fingerprint = event.data.fingerprint;
          patch.notice =
            event.data.message ?? "该操作有现实副作用，等待所有者确认。";
          // 同一指纹被拒后模型可能原样重试并再次通知；流通知比本地记账新，
          // 所以同时撤销“已决策”标记，否则回读会把新请求当作已处理。
          patch.decidedFingerprints = (turn.decidedFingerprints ?? []).filter(
            (item) => item !== fingerprint,
          );
          patch.confirmations = [
            ...(turn.confirmations ?? []).filter(
              (item) => item.fingerprint !== fingerprint,
            ),
            {
              run_id: event.data.run_id,
              fingerprint,
              tool: event.data.tool ?? "",
              arguments: event.data.arguments,
              message: event.data.message,
            },
          ];
          // The stream notice is a light signal; the REST list is the source of
          // truth and carries the full arguments the owner must review.
          void this.syncConfirmations(event.data.run_id);
        }
        break;
      case "step_started":
        patch.step = event.data.step;
        patch.status = "streaming";
        break;
      case "tool_started":
        patch.status = "streaming";
        patch.tools = [
          ...(turn.tools ?? []),
          {
            step: event.data.step,
            name: event.data.name,
            call_id: event.data.call_id,
            arguments: event.data.arguments,
          },
        ];
        break;
      case "tool_result":
        patch.tools = (turn.tools ?? []).map((tool) =>
          tool.call_id === event.data.call_id
            ? {
                ...tool,
                status: event.data.status,
                preview: event.data.preview,
                // A call released by full-access mode keeps that fact on the
                // record so the transcript can mark it as unconfirmed.
                ...(event.data.access === "full"
                  ? { access: "full" as const }
                  : {}),
                ...(event.data.verification
                  ? { verification: event.data.verification }
                  : {}),
                ...(event.data.execution
                  ? { execution: event.data.execution }
                  : {}),
              }
            : tool,
        );
        break;
      case "paused":
        // 后端在步边界补发的 paused 才是“真的暂停了”，请求标记随之结束。
        patch.paused = true;
        patch.pausePending = false;
        break;
      case "resumed":
        patch.paused = false;
        patch.resumePending = false;
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
        patch.verification = event.data.verification ?? turn.verification;
        patch.error = undefined;
        // 任何终态都清空暂停标记与确认卡，收尾后不残留待办。
        patch.paused = false;
        patch.pausePending = false;
        patch.resumePending = false;
        patch.confirmations = [];
        break;
      case "error":
        patch.status = event.data.code === "cancelled" ? "stopped" : "error";
        patch.error =
          event.data.code === "cancelled"
            ? undefined
            : chatRunErrorMessage(event.data.code);
        patch.paused = false;
        patch.pausePending = false;
        patch.resumePending = false;
        patch.confirmations = [];
        break;
    }
    // Keep rendering and replay cursors synchronous, but coalesce the growing
    // stream snapshot. started and terminal events remain immediate below.
    this.change(
      id,
      patch,
      !["started", "complete", "error"].includes(event.event),
    );
    if (event.event === "complete" || event.event === "error") {
      void this.refreshHistory();
      void this.syncRunState(event.data.run_id);
    }
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
    const turn = this.state.turns.find((item) => item.runId === run.id);
    const requestId = turn?.request.request_id ?? run.request_id;
    if (run.user_message_id)
      this.change(requestId, { userMessageId: run.user_message_id });
    this.change(requestId, {
      ...(run.verification ? { verification: run.verification } : {}),
      ...(typeof run.recoverable === "boolean"
        ? { recoverable: run.recoverable }
        : {}),
      ...(run.recovery_reason !== undefined
        ? { recoveryReason: run.recovery_reason }
        : {}),
    });
  }
  private async syncRunState(runId: string) {
    try {
      const run = await this.api.chatRun(runId);
      this.checkRun(run, runId);
      this.adoptRun(run);
    } catch {
      /* Stream terminal state remains useful when the status read is unavailable. */
    }
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
      verification: run.verification,
      recoverable: run.recoverable,
      recoveryReason: run.recovery_reason,
      // 通过 GET 状态收尾同样是终态：暂停标记与确认卡一并清空。
      paused: false,
      pausePending: false,
      resumePending: false,
      confirmations: [],
    };
  }
  recover = async (additionalSteps?: number): Promise<boolean> => {
    const turn = this.state.turns.at(-1);
    if (
      !turn?.runId ||
      turn.recoverable !== true ||
      !["error", "stopped"].includes(turn.status) ||
      this.recoveryRequest ||
      this.controller
    )
      return false;
    const id = turn.request.request_id;
    const runId = turn.runId;
    this.recoveryRequest = id;
    this.change(id, { recovering: true, error: undefined });
    try {
      const run = await this.api.recoverChat(runId, additionalSteps);
      this.checkRun(run, runId);
      this.adoptRun(run);
      if (run.status === "running") {
        this.change(id, {
          status: "connecting",
          error: undefined,
          recovering: false,
          recoverable: false,
          paused: false,
          pausePending: false,
          resumePending: false,
        });
        const current = this.state.turns.find(
          (item) => item.request.request_id === id,
        );
        if (current && this.attached) await this.connect(current);
        return true;
      }
      this.change(id, { ...this.runResult(run), recovering: false });
      void this.refreshHistory();
      return run.status === "completed";
    } catch (error) {
      this.change(id, {
        recovering: false,
        error: chatErrorMessage(error),
      });
      return false;
    } finally {
      if (this.recoveryRequest === id) this.recoveryRequest = null;
    }
  };
  refreshExecution = async (callId: string) => {
    const turn = this.state.turns.at(-1);
    const tool = turn?.tools?.find((item) => item.call_id === callId);
    const session = tool?.execution;
    if (!turn?.runId || !session?.session_id || !tool || tool.executionAction)
      return;
    const id = turn.request.request_id;
    this.patchExecutionTool(id, callId, { executionAction: "refreshing" });
    try {
      const latest = await this.api.executionSession(
        session.session_id,
        turn.runId,
        "chat",
        session.cursor,
      );
      const current = this.state.turns.find(
        (item) => item.request.request_id === id,
      );
      const previous = current?.tools?.find(
        (item) => item.call_id === callId,
      )?.execution;
      if (!previous) return;
      this.patchExecutionTool(id, callId, {
        execution: {
          ...previous,
          ...latest,
          output: (previous.output ?? "") + (latest.output ?? ""),
          truncated: previous.truncated || latest.truncated,
        },
      });
    } catch (error) {
      this.update({ actionError: errorMessage(error) });
    } finally {
      this.patchExecutionTool(id, callId, { executionAction: undefined });
    }
  };
  stopExecution = async (callId: string) => {
    const turn = this.state.turns.at(-1);
    const tool = turn?.tools?.find((item) => item.call_id === callId);
    const session = tool?.execution;
    if (!turn?.runId || !session?.session_id || !tool || tool.executionAction)
      return;
    const id = turn.request.request_id;
    this.patchExecutionTool(id, callId, { executionAction: "cancelling" });
    try {
      const result = await this.api.cancelExecutionSession(
        session.session_id,
        turn.runId,
        "chat",
      );
      this.patchExecutionTool(id, callId, {
        execution: {
          ...session,
          ...result,
          output: (session.output ?? "") + (result.output ?? ""),
          truncated: session.truncated || result.truncated,
        },
      });
    } catch (error) {
      this.update({ actionError: errorMessage(error) });
    } finally {
      this.patchExecutionTool(id, callId, { executionAction: undefined });
    }
  };
  private patchExecutionTool(
    id: string,
    callId: string,
    patch: Partial<ChatToolCall>,
  ) {
    const turn = this.state.turns.find(
      (item) => item.request.request_id === id,
    );
    if (!turn) return;
    this.change(id, {
      tools: (turn.tools ?? []).map((tool) =>
        tool.call_id === callId ? { ...tool, ...patch } : tool,
      ),
    });
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
          // stopped 也是终态：确认卡与暂停标记不能跨过停止残留下来。
          this.change(id, {
            status: "stopped",
            error: undefined,
            paused: false,
            pausePending: false,
            resumePending: false,
            confirmations: [],
          });
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
  // Loop intervention: pause/resume act at the next step boundary (never kill an
  // in-flight tool); a guide is injected before the next step.
  pauseRun = async (): Promise<void> => {
    const turn = this.state.turns.at(-1);
    if (!turn?.runId || turn.paused || turn.pausePending) return;
    const id = turn.request.request_id;
    // POST /pause 只置后端标志，真正暂停发生在下一个步边界；这里不乐观置位，
    // 只标“请求中”挡住重复点击，等 paused 事件落地再显示已暂停。
    this.change(id, { pausePending: true });
    try {
      await this.api.pauseChat(turn.runId);
    } catch (error) {
      this.change(id, { pausePending: false });
      this.update({ actionError: errorMessage(error) });
    }
  };
  resumeRun = async (): Promise<void> => {
    const turn = this.state.turns.at(-1);
    if (!turn?.runId || !turn.paused || turn.resumePending) return;
    const id = turn.request.request_id;
    // 与暂停同理：resumed 事件才是真的恢复，POST 期间只挡重复点击。
    this.change(id, { resumePending: true });
    try {
      await this.api.resumeChat(turn.runId);
    } catch (error) {
      this.change(id, { resumePending: false });
      this.update({ actionError: errorMessage(error) });
    }
  };
  guideRun = async (text: string): Promise<boolean> => {
    const turn = this.state.turns.at(-1);
    const value = text.trim();
    if (!turn?.runId || !value) return false;
    try {
      await this.api.guideChat(turn.runId, value);
      return true;
    } catch (error) {
      this.update({ actionError: errorMessage(error) });
      return false;
    }
  };
  decideConfirmation = async (
    fingerprint: string,
    approved: boolean,
  ): Promise<void> => {
    const turn = this.state.turns.at(-1);
    if (!turn?.runId) return;
    // 已记账的指纹直接短路：双击或并发触发不会发出第二个决策请求，
    // 也就不会用第二次 404 制造假错误横幅。
    if (turn.decidedFingerprints?.includes(fingerprint)) return;
    const id = turn.request.request_id;
    const runId = turn.runId;
    // 先记账再请求：POST 在途时到来的回读不能把该项复活。
    this.change(id, {
      decidedFingerprints: [...(turn.decidedFingerprints ?? []), fingerprint],
    });
    try {
      await this.api.decideChatConfirmation(runId, fingerprint, approved);
    } catch (error) {
      // 失败时卡片仍在、应允许重试，所以撤销记账。
      const failed = this.state.turns.find(
        (item) => item.request.request_id === id,
      );
      if (failed && failed.runId === runId)
        this.change(id, {
          decidedFingerprints: (failed.decidedFingerprints ?? []).filter(
            (item) => item !== fingerprint,
          ),
        });
      this.update({ actionError: errorMessage(error) });
      return;
    }
    // 只移除这一个指纹，且基于当前 state：await 期间由通知或回读写入的
    // 其它确认项必须原样保留，不能被旧快照整体覆盖。
    const current = this.state.turns.find(
      (item) => item.request.request_id === id,
    );
    if (current && current.runId === runId)
      this.change(id, {
        confirmations: (current.confirmations ?? []).filter(
          (item) => item.fingerprint !== fingerprint,
        ),
      });
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
  dispose = () => {
    this.suspend();
  };
  suspend = () => {
    for (const session of this.sessions.values()) session.suspend();
  };
}
