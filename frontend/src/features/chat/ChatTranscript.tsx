import { useEffect, useRef, useState } from "react";
import type { ChatSnapshot, ChatTurn } from "./session";
import type { Attachment, ConversationMessage } from "../../api/types";
import { AttachmentNames } from "./ChatAttachments";
import { EmptyState } from "../../shared/ui";

function Reply({ turn }: { turn: ChatTurn }) {
  return (
    <>
      {turn.thinking && (
        <details className="chat-thinking">
          <summary>思考过程</summary>
          <p className="preserve-text">{turn.thinking}</p>
        </details>
      )}
      {turn.text ? (
        <p className="preserve-text">{turn.text}</p>
      ) : (
        <p className="muted">
          {turn.status === "connecting"
            ? "正在连接…"
            : turn.status === "streaming"
              ? "正在等待回复…"
              : "尚未收到正文。"}
        </p>
      )}
      <div className="chat-message-meta metadata">
        <span>
          {turn.status === "complete"
            ? "回复完成"
            : turn.status === "stopped"
              ? "已停止 · 已收到的内容保留在此"
              : turn.status === "error"
                ? "回复未完成"
                : turn.status === "disconnected"
                  ? "连接已断开"
                  : "正在生成…"}
        </span>
        {turn.usage && (
          <span title="按服务商返回值展示；统计口径可能不同，总计可能不等于输入与输出之和。">
            {[
              typeof turn.usage.input_tokens === "number"
                ? "输入 " + turn.usage.input_tokens
                : "",
              typeof turn.usage.output_tokens === "number"
                ? "输出 " + turn.usage.output_tokens
                : "",
              typeof turn.usage.total_tokens === "number"
                ? "服务商总计 " + turn.usage.total_tokens
                : "",
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
        )}
      </div>
    </>
  );
}

function SavedMessage({
  message,
  name,
  turn,
  disabled,
  canRegenerate,
  onEdit,
  onRegenerate,
  attachments,
  regenerateIssue,
}: {
  message: ConversationMessage;
  name: string;
  turn?: ChatTurn;
  disabled: boolean;
  canRegenerate: boolean;
  onEdit: (id: string, content: string) => Promise<boolean>;
  onRegenerate: (id: string) => void;
  attachments: Attachment[];
  regenerateIssue?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(message.text);
  const user = message.role === "user";
  return (
    <article
      className={"chat-message " + (user ? "chat-user" : "chat-assistant")}
      aria-label={user ? "你的消息" : name + "的回复"}
      data-message-id={message.id}
      data-source-id={message.source.id}
      data-source-revision={message.source.revision}
    >
      <span className="chat-speaker">{user ? "你" : name}</span>
      {user && (
        <AttachmentNames
          ids={message.attachment_ids ?? []}
          attachments={attachments}
        />
      )}
      {editing ? (
        <form
          className="chat-edit"
          onSubmit={(event) => {
            event.preventDefault();
            if (!disabled && content.trim())
              void onEdit(message.id, content).then((saved) => {
                if (saved) setEditing(false);
              });
          }}
        >
          <label htmlFor={"edit-" + message.id}>编辑你的消息</label>
          <textarea
            id={"edit-" + message.id}
            value={content}
            onChange={(event) => setContent(event.target.value)}
            disabled={disabled}
            autoFocus
          />
          <p className="metadata">
            保存会创建新分支，原对话仍可从分支列表查看。
          </p>
          <div className="chat-message-actions">
            <button
              className="button secondary"
              type="submit"
              disabled={disabled || !content.trim()}
            >
              保存为新分支
            </button>
            <button
              className="text-button"
              type="button"
              disabled={disabled}
              onClick={() => setEditing(false)}
            >
              取消编辑
            </button>
          </div>
        </form>
      ) : (
        <>
          {turn ? (
            <Reply turn={{ ...turn, text: message.text }} />
          ) : (
            <p className="preserve-text">{message.text}</p>
          )}
          <div className="chat-message-actions">
            {user ? (
              <button
                className="text-button"
                disabled={disabled}
                onClick={() => {
                  setContent(message.text);
                  setEditing(true);
                }}
              >
                编辑消息
              </button>
            ) : (
              canRegenerate && (
                <button
                  className="text-button"
                  disabled={disabled || !!regenerateIssue}
                  title={regenerateIssue}
                  onClick={() => onRegenerate(message.id)}
                >
                  重新生成
                </button>
              )
            )}
          </div>
          {!user && regenerateIssue && (
            <p className="metadata">{regenerateIssue}</p>
          )}
        </>
      )}
    </article>
  );
}

export function ChatTranscript({
  state,
  name,
  disabled,
  canRegenerate,
  onEdit,
  onRegenerate,
  attachments = [],
  regenerateProblem,
}: {
  state: ChatSnapshot;
  name: string;
  disabled: boolean;
  canRegenerate: boolean;
  onEdit: (id: string, content: string) => Promise<boolean>;
  onRegenerate: (id: string) => void;
  attachments?: Attachment[];
  regenerateProblem?: (user: ConversationMessage) => string | undefined;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const last = state.turns.at(-1);
  const committed =
    last?.assistantSource &&
    state.messages.some(
      (message) =>
        message.source.id === last.assistantSource!.id &&
        message.source.revision === last.assistantSource!.revision,
    );
  const live =
    last &&
    !committed &&
    (last.status !== "complete" ||
      !state.conversationId ||
      state.historyStatus === "loading")
      ? last
      : undefined;
  const userVisible =
    live &&
    (live.request.regenerate_message_id ||
      state.messages.some((message) => message.id === live.userMessageId));
  useEffect(() => {
    if (follow.current && scroller.current)
      scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [state.messages, state.turns]);
  return (
    <div
      className="chat-transcript"
      ref={scroller}
      role="log"
      aria-label="本次对话"
      aria-live="polite"
      aria-relevant="additions text"
      onScroll={() => {
        const element = scroller.current!;
        follow.current =
          element.scrollHeight - element.scrollTop - element.clientHeight < 80;
      }}
    >
      {!state.messages.length && !live && state.historyStatus === "ready" && (
        <EmptyState title={"和" + name + "聊一聊"}>
          从一条消息开始。对话会保存在本地服务，重新打开后可继续。
        </EmptyState>
      )}
      {state.messages.map((message) => (
        <SavedMessage
          key={message.id}
          message={message}
          name={name}
          disabled={disabled}
          turn={state.turns.find(
            (turn) =>
              turn.assistantSource?.id === message.source.id &&
              turn.assistantSource.revision === message.source.revision,
          )}
          canRegenerate={
            canRegenerate &&
            state.messages.some(
              (user) => user.id === message.parent_id && user.role === "user",
            )
          }
          onEdit={onEdit}
          onRegenerate={onRegenerate}
          attachments={attachments}
          regenerateIssue={
            message.role === "assistant" &&
            state.messages.some(
              (user) => user.id === message.parent_id && user.role === "user",
            )
              ? regenerateProblem?.(
                  state.messages.find(
                    (user) =>
                      user.id === message.parent_id && user.role === "user",
                  )!,
                )
              : undefined
          }
        />
      ))}
      {live && (
        <div className="chat-turn" key={live.request.request_id}>
          {!userVisible && (
            <article className="chat-message chat-user" aria-label="你的消息">
              <span className="chat-speaker">你</span>
              <AttachmentNames
                ids={live.request.attachment_ids ?? []}
                attachments={attachments}
              />
              <p className="preserve-text">{live.request.message}</p>
            </article>
          )}
          <article
            className="chat-message chat-assistant"
            aria-label={name + "的回复"}
          >
            <span className="chat-speaker">{name}</span>
            <Reply turn={live} />
          </article>
        </div>
      )}
    </div>
  );
}
