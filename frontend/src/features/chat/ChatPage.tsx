import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import type { LingApi } from "../../api/client";
import type { Agent, Conversation } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading, PageHeader } from "../../shared/ui";
import { ChatComposer } from "./ChatComposer";
import { ChatControls } from "./ChatControls";
import { ChatTranscript } from "./ChatTranscript";
import { ConversationPicker } from "./ConversationPicker";
import { ChatSession, isPending } from "./session";
import { ChatAttachments, attachmentProblem } from "./ChatAttachments";
import { useChatAttachments } from "./useChatAttachments";
import { SegmentPanel } from "./SegmentPanel";
import "./chat.css";

export function ChatPage({
  api,
  agent,
  session: agentSession,
  openSettings,
}: {
  api: LingApi;
  agent: Agent;
  session: ChatSession;
  openSettings: () => void;
}) {
  const choiceKey = "lingnexus.chat.v1.choice." + agent.id;
  const [selectedId, setSelectedId] = useState<string | undefined>(() => {
    try {
      return (
        sessionStorage.getItem(choiceKey) ??
        agentSession.getSnapshot().conversationId
      );
    } catch {
      return agentSession.getSnapshot().conversationId;
    }
  });
  const [created, setCreated] = useState<Conversation>();
  const [creating, setCreating] = useState(false);
  const loadConversations = useCallback(
    (signal: AbortSignal) => api.conversations(agent.id, signal),
    [api, agent.id],
  );
  const conversations = useResource(loadConversations);
  const saved = conversations.status === "ready" ? conversations.data : [];
  const list =
    created && !saved.some((value) => value.id === created.id)
      ? [created, ...saved]
      : saved;
  const unresolved =
    isPending(agentSession.getSnapshot().turns.at(-1)) &&
    !agentSession.getSnapshot().conversationId;
  const conversationId = unresolved ? undefined : (selectedId ?? list[0]?.id);
  const session = conversationId
    ? agentSession.forConversation(conversationId)
    : agentSession;
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const choose = (id: string) => {
    setSelectedId(id);
    try {
      sessionStorage.setItem(choiceKey, id);
    } catch {
      /* Selection still works in memory. */
    }
  };
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
    // An ID-less initial send adopts the backend's persistent default without
    // changing its request fingerprint or replacing its active subscriber.
    if (state.conversationId && state.conversationId !== selectedId) {
      setSelectedId(state.conversationId);
      try {
        sessionStorage.setItem(choiceKey, state.conversationId);
      } catch {
        /* Optional storage. */
      }
      if (!list.some((value) => value.id === state.conversationId))
        void conversations.refresh();
    }
  }, [
    state.conversationId,
    selectedId,
    choiceKey,
    list,
    conversations.refresh,
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
  const locked = pending || state.busy || creating || attachments.uploading;
  const disabled =
    locked || historyUnavailable || conversations.status !== "ready";
  const send = () => session.send(state.draft, profileId, effort);
  return (
    <div className="page chat-page">
      <PageHeader
        eyebrow="对话"
        title={"与" + agent.name + "对话"}
        description="说说此刻的想法，让对话慢慢展开。"
        action={
          <button className="button secondary" onClick={openSettings}>
            模型设置
          </button>
        }
      />
      {state.conversationId && <SegmentPanel key={state.conversationId} api={api} conversationId={state.conversationId} messages={state.messages} disabled={locked}/>}
      <section className="panel chat-panel" aria-label="聊天">
        <ConversationPicker
          api={api}
          agentId={agent.id}
          conversations={list}
          conversationId={conversationId ?? ""}
          branches={state.branches}
          disabled={locked}
          loading={conversations.status !== "ready"}
          onSelect={choose}
          onBranch={(id) => {
            if (!disabled) void session.selectBranch(id);
          }}
          onCreated={(value) => {
            setCreated(value);
            choose(value.id);
            void conversations.refresh();
          }}
          onCreating={setCreating}
        />
        {conversations.status === "loading" && (
          <Loading label="正在读取保存的对话…" />
        )}
        {conversations.status === "error" && (
          <ErrorNotice
            message={conversations.error}
            retry={() => void conversations.refresh()}
          />
        )}
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
          attachments={
            <ChatAttachments
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
    </div>
  );
}
