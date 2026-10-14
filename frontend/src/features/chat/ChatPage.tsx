import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { Folder, Monitor, ShieldAlert } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";
import { ChatAttachments, attachmentProblem } from "./ChatAttachments";
import { ChatComposer } from "./ChatComposer";
import { ChatControls } from "./ChatControls";
import { ChatHeader } from "./ChatHeader";
import { ChatRunControls } from "./ChatRunControls";
import { ChatTranscript } from "./ChatTranscript";
import { ChatSession, isPending } from "./session";
import type { ConversationList } from "./useConversations";
import { useChatAttachments } from "./useChatAttachments";
import { TasksPage } from "../tasks/TasksPage";
import "./chat.css";

export function ChatPage({
  api,
  agent,
  session: agentSession,
  conversations,
  openSettings,
  openBrowser,
}: {
  api: LingApi;
  agent: Agent;
  session: ChatSession;
  conversations: ConversationList;
  openSettings: () => void;
  openBrowser: () => void;
}) {
  const [attachmentsOpen, setAttachmentsOpen] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState(new Set<string>());
  const [confirming, setConfirming] = useState<string[]>([]);
  const [deciding, setDeciding] = useState<string[]>([]);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  // 完全访问（按对话）：读取失败按安全默认「逐条确认」显示，不打扰对话。
  const [access, setAccess] = useState<"confirm" | "full">("confirm");
  const [accessBusy, setAccessBusy] = useState(false);
  const [accessPrompt, setAccessPrompt] = useState(false);
  const [accessError, setAccessError] = useState("");
  const [workdirName, setWorkdirName] = useState("");
  const list = conversations.list;
  const unresolved =
    isPending(agentSession.getSnapshot().turns.at(-1)) &&
    !agentSession.getSnapshot().conversationId;
  const conversationId = unresolved
    ? undefined
    : (conversations.selectedId ?? list[0]?.id);
  const session = conversationId
    ? agentSession.forConversation(conversationId)
    : agentSession;
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const load = useCallback(
    async (signal: AbortSignal) => {
      const [profiles, selection] = await Promise.all([
        api.models(signal),
        api.modelSelection(signal),
      ]);
      return { profiles, selection };
    },
    [api],
  );
  const models = useResource(load);
  useEffect(session.attach, [session]);
  useEffect(() => {
    let current = true;
    api
      .permissions(agent.id)
      .then((value) => {
        if (!current) return;
        const root = value.writable[0] ?? value.readable[0];
        setWorkdirName(
          root ? (root.replace(/[\\/]+$/, "").split(/[\\/]/).pop() ?? "") : "",
        );
      })
      .catch(() => {
        /* 读不到授权目录时省略这一枚标签。 */
      });
    return () => {
      current = false;
    };
  }, [api, agent.id]);
  useEffect(() => {
    if (!conversationId) {
      setAccess("confirm");
      return;
    }
    const controller = new AbortController();
    setAccessError("");
    api
      .conversationAccess(conversationId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setAccess(value.mode);
      })
      .catch(() => {
        /* 读取失败按安全默认显示；真正的失败会在开启动作里如实报出。 */
      });
    return () => controller.abort();
  }, [api, conversationId]);
  useEffect(() => {
    // An ID-less initial send adopts the backend's persistent default without
    // changing its request fingerprint or replacing its active subscriber.
    if (
      state.conversationId &&
      state.conversationId !== conversations.selectedId
    ) {
      conversations.choose(state.conversationId);
      if (!list.some((value) => value.id === state.conversationId)) {
        void conversations.refresh();
      }
    }
  }, [
    state.conversationId,
    conversations.selectedId,
    conversations.choose,
    conversations.refresh,
    list,
  ]);
  const last = state.turns.at(-1);
  const pending = isPending(last);
  const profiles = models.status === "ready" ? models.data.profiles : [];
  const profileId = profiles.some((item) => item.id === state.profileId)
    ? state.profileId!
    : models.status === "ready" &&
        profiles.some((item) => item.id === models.data.selection.primary)
      ? models.data.selection.primary!
      : "";
  const profile = profiles.find((item) => item.id === profileId);
  const effortLevels = profile?.profile.capabilities.effort_levels ?? [];
  const effort = effortLevels.includes(state.effort) ? state.effort : "";
  const selected = list.find((value) => value.id === conversationId);
  const attachments = useChatAttachments(api, agent.id, session, [
    ...state.attachmentIds,
    ...state.messages.flatMap((message) => message.attachment_ids ?? []),
    ...(last?.request.attachment_ids ?? []),
  ]);
  const attachmentIssue = attachmentProblem(
    state.attachmentIds,
    attachments.items,
    profile?.profile.capabilities,
    profile?.profile.api_family,
  );
  const historyUnavailable =
    !!state.conversationId && state.historyStatus !== "ready";
  const locked =
    pending || state.busy || conversations.creating || attachments.uploading;
  const disabled = locked || historyUnavailable || conversations.loading;
  const send = () => session.send(state.draft, profileId, effort);
  const togglePick = (id: string) =>
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const stopSelecting = () => {
    setSelecting(false);
    setPicked(new Set());
  };
  const runDelete = async (anchors: string[]) => {
    setConfirming([]);
    const done = await session.deleteTurns(anchors);
    if (done) stopSelecting();
  };
  const changeAccess = async (mode: "confirm" | "full"): Promise<boolean> => {
    if (!conversationId || accessBusy) return false;
    setAccessBusy(true);
    setAccessError("");
    try {
      const value = await api.setConversationAccess(conversationId, mode);
      setAccess(value.mode);
      return true;
    } catch (error) {
      setAccessError(errorMessage(error));
      return false;
    } finally {
      setAccessBusy(false);
    }
  };
  const decide = (fingerprint: string, approved: boolean) => {
    // 在途的同一指纹直接忽略；会话层还有已决策记账兜底，双击不会造成
    // 第二次决策请求（也就不会用 404 制造假错误横幅）。
    if (deciding.includes(fingerprint)) return;
    setDeciding((current) => [...current, fingerprint]);
    void session
      .decideConfirmation(fingerprint, approved)
      .finally(() =>
        setDeciding((current) =>
          current.filter((item) => item !== fingerprint),
        ),
      );
  };
  const userTurns = state.messages.filter(
    (message) => message.role === "user",
  ).length;
  return (
    <div className="page chat-page">
      <section className="panel chat-panel" aria-label="聊天">
        <ChatHeader
          title={selected?.title ?? "新对话"}
          selecting={selecting}
          canSelect={!!userTurns && !disabled}
          onSelectTurns={() => (selecting ? stopSelecting() : setSelecting(true))}
          onOpenSettings={openSettings}
          onOpenWorkspace={() => setWorkspaceOpen(true)}
          disabled={locked}
        />
        {access === "full" && (
          <div className="chat-banner" role="status">
            <ShieldAlert size={14} aria-hidden="true" />
            <span>
              完全访问已开启 · 本对话内命令与远程工具不再逐条确认（沙箱、审计与目录授权不变）
            </span>
            <button
              type="button"
              className="chat-banner-close"
              disabled={accessBusy}
              onClick={() => void changeAccess("confirm")}
            >
              立即关闭
            </button>
          </div>
        )}
        {accessError && <ErrorNotice message={accessError} />}
        <div className="chat-column">
        {models.status === "loading" && <Loading label="正在读取对话模型…" />}
        {models.status === "error" && (
          <ErrorNotice
            message={models.error}
            retry={() => void models.refresh()}
          />
        )}
        {models.status === "ready" && !profiles.length && (
          <p className="notice">
            还没有模型连接。请到模型设置中添加连接并选择主用模型。
          </p>
        )}
        {models.status === "ready" && !!profiles.length && !profileId && (
          <p className="notice">
            尚未选择主用模型，请在下方输入框右下角选择本次对话使用的连接。
          </p>
        )}
        {profile && !profile.credentials_available && (
          <p className="notice">
            当前模型尚未提供密钥，请到模型设置中填写本次运行的模型密钥。
          </p>
        )}
        {state.historyStatus === "loading" && (
          <Loading label="正在读取对话消息…" />
        )}
        {state.historyError && (
          <ErrorNotice
            message={state.historyError}
            retry={() => void session.refreshHistory()}
          />
        )}
        {state.actionError && <ErrorNotice message={state.actionError} />}
        {(last?.switchedProfile || last?.notice) && (
          <p className="notice" role="status">
            {last.switchedProfile && (
              <>
                已切换到备用模型
                {profiles.find((profile) => profile.id === last.switchedProfile)
                  ?.name
                  ? "「" +
                    profiles.find(
                      (profile) => profile.id === last.switchedProfile,
                    )!.name +
                    "」"
                  : ""}
                。{pending ? "正在重新生成回复。" : ""}
              </>
            )}
            {last.notice}
          </p>
        )}
        <ChatTranscript
          state={state}
          name={agent.name}
          disabled={disabled}
          canRegenerate={!!profileId && models.status === "ready"}
          onEdit={session.editMessage}
          onRegenerate={(id) => session.regenerate(id, profileId, effort)}
          onDeleteTurn={(id) => setConfirming([id])}
          selecting={selecting}
          selected={picked}
          onToggleSelect={togglePick}
          attachments={attachments.items}
          regenerateProblem={(user) =>
            attachmentProblem(
              user.attachment_ids ?? [],
              attachments.items,
              profile?.profile.capabilities,
              profile?.profile.api_family,
            )
          }
        />
        {last?.error && <ErrorNotice message={last.error} />}
        {last?.status === "disconnected" && (
          <div className="chat-recovery">
            <p className="metadata">
              重新连接会接续当前生成，不会重复发送消息。
            </p>
            <button
              className="button secondary"
              onClick={() => void session.reconnect()}
            >
              重新连接
            </button>
          </div>
        )}
        {last?.status === "error" && (
          <div className="chat-recovery">
            <p className="metadata">
              检查模型设置后，可将同一条消息作为新请求再次发送。
            </p>
            <button
              className="button secondary"
              disabled={
                !profileId ||
                disabled ||
                !!attachmentProblem(
                  last.request.attachment_ids ?? [],
                  attachments.items,
                  profile?.profile.capabilities,
                  profile?.profile.api_family,
                )
              }
              onClick={() => session.retry(profileId, effort)}
            >
              再次发送
            </button>
          </div>
        )}
        {selecting && (
          <div className="chat-select-bar">
            <span>已选 {picked.size} 轮</span>
            <button className="button secondary" onClick={stopSelecting}>
              取消
            </button>
            <button
              className="button danger"
              disabled={!picked.size}
              onClick={() => setConfirming([...picked])}
            >
              删除所选
            </button>
          </div>
        )}
        </div>
        {(last?.confirmations?.length ?? 0) > 0 && (
          <div className="chat-approval" role="alert">
            {last!.confirmations!.map((item) => (
              <section
                className="panel chat-approval-card"
                key={item.fingerprint}
              >
                <h2>需要你确认的一次操作</h2>
                <p>
                  {agent.name}请求执行「<strong>{item.tool}</strong>」
                  。该操作有现实副作用，批准后才会真正执行。
                </p>
                {item.arguments &&
                  Object.keys(item.arguments).length > 0 && (
                    <pre className="chat-approval-args">
                      {JSON.stringify(item.arguments, null, 2)}
                    </pre>
                  )}
                <div className="button-row">
                  <button
                    className="button secondary"
                    disabled={deciding.includes(item.fingerprint)}
                    onClick={() => decide(item.fingerprint, false)}
                  >
                    拒绝
                  </button>
                  <button
                    className="button"
                    disabled={deciding.includes(item.fingerprint)}
                    onClick={() => decide(item.fingerprint, true)}
                  >
                    批准并执行
                  </button>
                </div>
              </section>
            ))}
          </div>
        )}
        {pending && last && last.runId && (
          <ChatRunControls
            paused={!!last.paused}
            pausePending={!!last.pausePending}
            resumePending={!!last.resumePending}
            onPause={() => void session.pauseRun()}
            onResume={() => void session.resumeRun()}
            onGuide={session.guideRun}
            disabled={false}
          />
        )}
        <ChatComposer
          draft={state.draft}
          onDraft={session.setDraft}
          onSend={send}
          disabled={
            !profileId ||
            models.status !== "ready" ||
            disabled ||
            !!attachmentIssue
          }
          attachmentsOpen={attachmentsOpen}
          onToggleAttachments={() => setAttachmentsOpen(!attachmentsOpen)}
          attachments={
            <ChatAttachments
              open={attachmentsOpen}
              attachments={attachments.items}
              selected={state.attachmentIds}
              onSelect={session.setAttachments}
              disabled={locked}
              loading={attachments.loading}
              error={attachments.error}
              refresh={attachments.refresh}
              upload={(file) => {
                if (!locked) void attachments.upload(file);
              }}
              uploading={attachments.uploading}
              uploadError={attachments.uploadError}
              problem={attachmentIssue}
            />
          }
          pending={pending}
          canStop={!!last?.runId}
          cancelling={!!last?.cancelling}
          onStop={() => void session.stop()}
          access={
            <button
              type="button"
              className={"chat-access" + (access === "full" ? " on" : "")}
              aria-pressed={access === "full"}
              disabled={accessBusy || !conversationId}
              title={
                access === "full"
                  ? "关闭完全访问"
                  : "开启完全访问：本对话内命令与远程工具不再逐条确认"
              }
              onClick={() => {
                if (access === "full") void changeAccess("confirm");
                else setAccessPrompt(true);
              }}
            >
              <ShieldAlert size={13} aria-hidden="true" />
              完全访问
            </button>
          }
          context={
            <div className="chat-context-chips">
              {workdirName && (
                <span>
                  <Folder size={12} aria-hidden="true" />
                  {workdirName}
                </span>
              )}
              <span>
                <Monitor size={12} aria-hidden="true" />
                本机
              </span>
            </div>
          }
          controls={
            <ChatControls
              profiles={profiles}
              profileId={profileId}
              onProfile={session.setProfile}
              effortLevels={effortLevels}
              effort={effort}
              onEffort={session.setEffort}
              disabled={
                pending || models.status !== "ready" || !profiles.length
              }
              locked={locked}
              loading={models.status === "loading"}
              onRefresh={() => void models.refresh()}
            />
          }
        />
      </section>
      {confirming.length > 0 && (
        <div
          className="chat-confirm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="chat-confirm-heading"
        >
          <section className="panel chat-confirm-card">
            <h2 id="chat-confirm-heading">删除这一轮？</h2>
            <p>
              将删除 {confirming.length} 轮。这些内容会从对话中消失，之后发给模型的
              请求里不再出现，删除后无法撤销。
            </p>
            <div className="button-row">
              <button
                className="button secondary"
                onClick={() => setConfirming([])}
              >
                取消
              </button>
              <button
                className="button danger"
                onClick={() => void runDelete(confirming)}
              >
                确认删除
              </button>
            </div>
          </section>
        </div>
      )}
      {accessPrompt && (
        <ConfirmDialog
          title="开启「完全访问」？"
          confirmLabel="确认开启"
          busy={accessBusy}
          onCancel={() => setAccessPrompt(false)}
          onConfirm={() => {
            void changeAccess("full").then((saved) => {
              if (saved) setAccessPrompt(false);
            });
          }}
        >
          <p>
            开启后，本条对话内：执行命令、远程工具不再逐条向你确认。沙箱限制、审计记录、目录授权、浏览器点击审批保持不变。
          </p>
          <p>
            风险：这道人工确认被移除后，若灵读到恶意指令，可能在本机已授权的范围内执行危险操作。建议用完立即关闭。
          </p>
        </ConfirmDialog>
      )}
      {workspaceOpen && (
        <div
          className="chat-confirm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="chat-workspace-heading"
        >
          <section className="panel chat-workspace-card">
            <div className="section-heading">
              <h2 id="chat-workspace-heading">任务工作副本</h2>
              <button
                className="text-button"
                onClick={() => setWorkspaceOpen(false)}
              >
                关闭
              </button>
            </div>
            <p className="muted small">
              日常多步工作直接在对话里说即可，不必另建任务。这里保留"先看变更再写回"的流程：
              需要审阅文件改动、确认后再写回项目时，在此创建任务并查看实际执行记录。
            </p>
            <TasksPage
              api={api}
              agent={agent}
              openBrowser={openBrowser}
              embedded
            />
          </section>
        </div>
      )}
    </div>
  );
}
