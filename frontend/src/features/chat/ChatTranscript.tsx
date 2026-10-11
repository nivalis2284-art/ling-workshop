import { useEffect, useRef, useState } from "react";
import type { ChatSnapshot, ChatTurn } from "./session";
import type {
  Attachment,
  ChatToolCall,
  ConversationMessage,
} from "../../api/types";
import { AttachmentNames } from "./ChatAttachments";
import { EmptyState } from "../../shared/ui";

// The tool name is kept alongside the plain-language label so the owner can
// always see exactly which primitive ran, never just a friendly paraphrase.
const TOOL_LABELS: Record<string, string> = {
  run_command: "运行命令",
  read_file: "读取文件",
  create_file: "新建文件",
  write_file: "写入文件",
  edit_file: "编辑文件",
  search: "查找内容",
  browser_goto: "打开网页",
  browser_read: "读取网页",
  browser_fill: "填写表单",
  browser_request_click: "点击网页",
  browser_state: "查看浏览器",
  request_dependencies: "申请依赖",
};

function toolLabel(name: string) {
  const label = TOOL_LABELS[name];
  return label ? label + "（" + name + "）" : name;
}

function toolStatus(tool: ChatToolCall) {
  switch (tool.status) {
    case undefined:
      return "执行中…";
    case "completed":
      return "完成";
    case "blocked":
      return "已拒绝";
    case "failed":
      return "失败";
    case "unknown":
      return "结果待核对";
    default:
      return tool.status;
  }
}

function ToolCalls({ tools }: { tools?: ChatToolCall[] }) {
  if (!tools?.length) return null;
  return (
    <ul className="chat-tools" aria-label="工具调用">
      {tools.map((tool) => (
        <li
          key={tool.call_id}
          className={"chat-tool" + (tool.status ? " " + tool.status : "")}
        >
          <div className="chat-tool-head">
            <span className="chat-tool-name">{toolLabel(tool.name)}</span>
            <span className="chat-tool-status">{toolStatus(tool)}</span>
          </div>
          {tool.preview && (
            <pre className="chat-tool-preview">{tool.preview}</pre>
          )}
        </li>
      ))}
    </ul>
  );
}

function Reply({ turn }: { turn: ChatTurn }) {
  return (
    <>
      {turn.thinking && (
        <details className="chat-thinking">
          <summary>思考过程</summary>
          <p className="preserve-text">{turn.thinking}</p>
        </details>
      )}
      <ToolCalls tools={turn.tools} />
      {turn.text ? (
        <p className="preserve-text">{turn.text}</p>
      ) : (
        <p className="muted">
          {turn.status === "connecting"
            ? "正在连接…"
            : turn.status === "streaming"
              ? turn.paused
                ? "已暂停，等待继续。"
                : "正在等待回复…"
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
                  : turn.paused
                    ? "已暂停"
                    : turn.step
                      ? "正在生成…（第 " + turn.step + " 步）"
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
  onDeleteTurn,
  selecting,
  selected,
  onToggleSelect,
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
  onDeleteTurn?: (id: string) => void;
  selecting: boolean;
  selected: boolean;
  onToggleSelect: (id: string) => void;
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
    >
      <div className="chat-message-head">
        {selecting && user && (
          <label className="chat-select">
            <input
              type="checkbox"
              checked={selected}
              disabled={disabled}
              aria-label={"选择这一轮：" + message.text.slice(0, 24)}
              onChange={() => onToggleSelect(message.id)}
            />
          </label>
        )}
        <span className="chat-speaker">{user ? "你" : name}</span>
      </div>
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
            保存会从这里重新生成回复，原来的说法仍保留在记录中。
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
              <>
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
                {onDeleteTurn && (
                  <button
                    className="text-button danger"
                    disabled={disabled}
                    onClick={() => onDeleteTurn(message.id)}
                  >
                    删除这一轮
                  </button>
                )}
              </>
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
  onDeleteTurn,
  selecting = false,
  selected = new Set<string>(),
  onToggleSelect = () => {},
  attachments = [],
  regenerateProblem,
}: {
  state: ChatSnapshot;
  name: string;
  disabled: boolean;
  canRegenerate: boolean;
  onEdit: (id: string, content: string) => Promise<boolean>;
  onRegenerate: (id: string) => void;
  onDeleteTurn?: (id: string) => void;
  selecting?: boolean;
  selected?: Set<string>;
  onToggleSelect?: (id: string) => void;
  attachments?: Attachment[];
  regenerateProblem?: (user: ConversationMessage) => string | undefined;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const last = state.turns.at(-1);
  const committed =
    !!last?.assistantMessageId &&
    state.messages.some((message) => message.id === last.assistantMessageId);
  // 空正文的完成没有可挂靠的已存消息（assistant_message_id 可为 null）；
  // 保留 live 回合显示占位，而不是让整轮静默消失。
  const emptyReply = last?.status === "complete" && !last.assistantMessageId;
  const live =
    last &&
    !committed &&
    (emptyReply ||
      last.status !== "complete" ||
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
            (turn) => turn.assistantMessageId === message.id,
          )}
          canRegenerate={
            canRegenerate &&
            state.messages.some(
              (user) => user.id === message.parent_id && user.role === "user",
            )
          }
          onEdit={onEdit}
          onRegenerate={onRegenerate}
          onDeleteTurn={onDeleteTurn}
          selecting={selecting}
          selected={selected.has(message.id)}
          onToggleSelect={onToggleSelect}
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
