import { useEffect, useRef, useState } from "react";
import { Check, Copy, Pencil, RefreshCw, Trash2 } from "lucide-react";
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
  if (tool.execution) {
    if (tool.execution.status === "running") return "命令运行中";
    if (tool.execution.status === "cancelled") return "命令已停止";
    if (tool.execution.status === "unknown") return "命令结果待核对";
    if (tool.execution.status === "completed") return "命令已退出";
  }
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

function toolMark(tool: ChatToolCall) {
  if (tool.execution?.status === "running") return "…";
  if (tool.status === "completed")
    return <Check size={9} strokeWidth={3} aria-hidden="true" />;
  if (tool.status === "blocked" || tool.status === "failed") return "!";
  if (tool.status === "unknown") return "?";
  return "…";
}

/** One turn's tool activity as a grouped record card. */
function verificationLabel(status: string, kind?: string) {
  if (status === "passed")
    return kind === "content"
      ? "内容已回读"
      : kind === "command"
        ? "检查通过"
        : "验证通过";
  if (status === "stale") return "验证已失效";
  if (status === "failed") return "检查失败";
  if (status === "unknown") return "结果未知";
  return "尚未验证";
}

function ToolCalls({
  tools,
  onRefreshExecution,
  onStopExecution,
}: {
  tools?: ChatToolCall[];
  onRefreshExecution?: (callId: string) => void;
  onStopExecution?: (callId: string) => void;
}) {
  if (!tools?.length) return null;
  const settled = tools.filter(
    (tool) =>
      tool.status &&
      tool.status !== "running" &&
      tool.execution?.status !== "running",
  ).length;
  const finished = tools.filter(
    (tool) =>
      tool.status === "completed" && tool.execution?.status !== "running",
  ).length;
  return (
    <section className="chat-tools" aria-label="工具调用">
      <div className="chat-tools-head">
        <span className="chat-tools-title">工作记录</span>
        <span className="chat-tools-meta">
          {tools.length} 项
          {settled < tools.length
            ? " · 进行中"
            : finished === tools.length
              ? " · 全部完成"
              : ""}
        </span>
      </div>
      <ul className="chat-tools-list">
        {tools.map((tool) => (
          <li
            key={tool.call_id}
            className={"chat-tool" + (tool.status ? " " + tool.status : "")}
          >
            <span className="chat-tool-mark" aria-hidden="true">
              {toolMark(tool)}
            </span>
            <span className="chat-tool-name">{toolLabel(tool.name)}</span>
            {tool.access === "full" && (
              <span
                className="chat-tool-access"
                title="本条对话的完全访问模式放行，未逐条确认"
              >
                免确认
              </span>
            )}
            <span className="chat-tool-status">{toolStatus(tool)}</span>
            {tool.verification && (
              <span className="chat-verification" role="status">
                验证状态：
                {verificationLabel(
                  tool.verification.status,
                  tool.verification.kind,
                )}
                {tool.verification.checks?.map((check, index) => (
                  <span className="chat-verification-check" key={index}>
                    {check.argv.join(" ")}（退出码 {check.exit_code}）
                  </span>
                ))}
              </span>
            )}
            {tool.preview && (
              <pre className="chat-tool-preview">{tool.preview}</pre>
            )}
            {tool.execution?.output && (
              <pre className="chat-tool-preview">{tool.execution.output}</pre>
            )}
            {tool.execution?.result && (
              <pre className="chat-tool-preview">
                {JSON.stringify(tool.execution.result, null, 2)}
              </pre>
            )}
            {tool.execution?.truncated && (
              <span className="chat-tool-status">输出已截断</span>
            )}
            {tool.execution?.session_id &&
              tool.execution.status === "running" && (
                <div className="chat-execution-controls">
                  <button
                    type="button"
                    className="text-button"
                    disabled={!!tool.executionAction}
                    onClick={() => onRefreshExecution?.(tool.call_id)}
                  >
                    {tool.executionAction === "refreshing"
                      ? "刷新中…"
                      : "刷新命令输出"}
                  </button>
                  <button
                    type="button"
                    className="text-button danger"
                    disabled={!!tool.executionAction}
                    onClick={() => onStopExecution?.(tool.call_id)}
                  >
                    {tool.executionAction === "cancelling"
                      ? "正在停止…"
                      : "停止命令"}
                  </button>
                </div>
              )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Hover toolbar under one message: copy for everyone, edit for the owner's
 * messages, regenerate for replies, and delete-the-turn on either side. */
function MessageTools({
  text,
  user,
  disabled,
  canRegenerate,
  deleteAnchor,
  regenerateIssue,
  onEdit,
  onRegenerate,
  onDeleteTurn,
}: {
  text: string;
  user: boolean;
  disabled: boolean;
  canRegenerate: boolean;
  deleteAnchor?: string;
  regenerateIssue?: string;
  onEdit: () => void;
  onRegenerate: () => void;
  onDeleteTurn?: (id: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    try {
      const clipboard = navigator.clipboard;
      if (!clipboard?.writeText) return;
      void clipboard.writeText(text).then(
        () => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1400);
        },
        () => {},
      );
    } catch {
      /* 剪贴板不可用时保持静默。 */
    }
  };
  return (
    <div className="chat-msgtools" role="toolbar" aria-label="消息操作">
      <button
        type="button"
        className="chat-msgbtn"
        aria-label="复制消息"
        title={copied ? "已复制" : "复制"}
        onClick={copy}
      >
        {copied ? (
          <Check size={13} aria-hidden="true" />
        ) : (
          <Copy size={13} aria-hidden="true" />
        )}
      </button>
      {user && (
        <button
          type="button"
          className="chat-msgbtn"
          aria-label="编辑消息"
          title="编辑"
          disabled={disabled}
          onClick={onEdit}
        >
          <Pencil size={13} aria-hidden="true" />
        </button>
      )}
      {!user && canRegenerate && (
        <button
          type="button"
          className="chat-msgbtn"
          aria-label="重新生成"
          title={regenerateIssue ?? "重新生成"}
          disabled={disabled || !!regenerateIssue}
          onClick={onRegenerate}
        >
          <RefreshCw size={13} aria-hidden="true" />
        </button>
      )}
      {onDeleteTurn && deleteAnchor && (
        <button
          type="button"
          className="chat-msgbtn danger"
          aria-label="删除这一轮"
          title="删除这一轮"
          disabled={disabled}
          onClick={() => onDeleteTurn(deleteAnchor)}
        >
          <Trash2 size={13} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

function Reply({
  turn,
  onRefreshExecution,
  onStopExecution,
}: {
  turn: ChatTurn;
  onRefreshExecution?: (callId: string) => void;
  onStopExecution?: (callId: string) => void;
}) {
  return (
    <>
      {turn.thinking && (
        <details className="chat-thinking">
          <summary>思考过程</summary>
          <p className="preserve-text">{turn.thinking}</p>
        </details>
      )}
      <ToolCalls
        tools={turn.tools}
        onRefreshExecution={onRefreshExecution}
        onStopExecution={onStopExecution}
      />
      {turn.verification && (
        <div className="chat-verification" role="status">
          验证状态：
          {verificationLabel(turn.verification.status, turn.verification.kind)}
          {turn.verification.checks?.map((check, index) => (
            <span className="chat-verification-check" key={index}>
              {check.argv.join(" ")}（退出码 {check.exit_code}）
            </span>
          ))}
        </div>
      )}
      {turn.text ? (
        <p className="preserve-text chat-bubble">{turn.text}</p>
      ) : (
        <p className="muted chat-hint">
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
  deleteAnchor,
  onRefreshExecution,
  onStopExecution,
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
  deleteAnchor?: string;
  onRefreshExecution?: (callId: string) => void;
  onStopExecution?: (callId: string) => void;
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
      <div className="chat-row">
        {!user && (
          <span className="chat-avatar" aria-hidden="true">
            {name.slice(0, 1)}
          </span>
        )}
        <div className="chat-stack">
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
                <Reply
                  turn={{ ...turn, text: message.text }}
                  onRefreshExecution={onRefreshExecution}
                  onStopExecution={onStopExecution}
                />
              ) : (
                <p className="preserve-text chat-bubble">{message.text}</p>
              )}
              {!user && regenerateIssue && (
                <p className="metadata">{regenerateIssue}</p>
              )}
              <MessageTools
                text={message.text}
                user={user}
                disabled={disabled}
                canRegenerate={canRegenerate}
                deleteAnchor={deleteAnchor}
                regenerateIssue={regenerateIssue}
                onEdit={() => {
                  setContent(message.text);
                  setEditing(true);
                }}
                onRegenerate={() => onRegenerate(message.id)}
                onDeleteTurn={onDeleteTurn}
              />
            </>
          )}
        </div>
      </div>
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
  onRefreshExecution,
  onStopExecution,
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
  onRefreshExecution?: (callId: string) => void;
  onStopExecution?: (callId: string) => void;
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
      {state.messages.map((message) => {
        const parentUser = state.messages.find(
          (item) => item.id === message.parent_id && item.role === "user",
        );
        return (
          <SavedMessage
            key={message.id}
            message={message}
            name={name}
            disabled={disabled}
            turn={state.turns.find(
              (turn) => turn.assistantMessageId === message.id,
            )}
            canRegenerate={canRegenerate && !!parentUser}
            onEdit={onEdit}
            onRegenerate={onRegenerate}
            onDeleteTurn={onDeleteTurn}
            selecting={selecting}
            selected={selected.has(message.id)}
            onToggleSelect={onToggleSelect}
            attachments={attachments}
            deleteAnchor={message.role === "user" ? message.id : parentUser?.id}
            regenerateIssue={
              message.role === "assistant" && parentUser
                ? regenerateProblem?.(parentUser)
                : undefined
            }
            onRefreshExecution={onRefreshExecution}
            onStopExecution={onStopExecution}
          />
        );
      })}
      {live && (
        <div className="chat-turn" key={live.request.request_id}>
          {!userVisible && (
            <article className="chat-message chat-user" aria-label="你的消息">
              <div className="chat-row">
                <div className="chat-stack">
                  <AttachmentNames
                    ids={live.request.attachment_ids ?? []}
                    attachments={attachments}
                  />
                  <p className="preserve-text chat-bubble">
                    {live.request.message}
                  </p>
                </div>
              </div>
            </article>
          )}
          <article
            className="chat-message chat-assistant"
            aria-label={name + "的回复"}
          >
            <div className="chat-row">
              <span className="chat-avatar" aria-hidden="true">
                {name.slice(0, 1)}
              </span>
              <div className="chat-stack">
                <Reply
                  turn={live}
                  onRefreshExecution={onRefreshExecution}
                  onStopExecution={onStopExecution}
                />
              </div>
            </div>
          </article>
        </div>
      )}
    </div>
  );
}
