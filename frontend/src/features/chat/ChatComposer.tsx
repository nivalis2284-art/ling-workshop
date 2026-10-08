import { useRef } from "react";
import type { ReactNode } from "react";
import { Send, Square } from "lucide-react";

export function ChatComposer({
  draft,
  onDraft,
  onSend,
  disabled,
  pending,
  canStop,
  cancelling,
  onStop,
  attachments,
}: {
  draft: string;
  onDraft: (value: string) => void;
  onSend: () => void;
  disabled: boolean;
  pending: boolean;
  canStop: boolean;
  cancelling: boolean;
  onStop: () => void;
  attachments?: ReactNode;
}) {
  const composing = useRef(false);
  return (
    <form
      className="chat-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled && !pending && draft.trim()) onSend();
      }}
    >
      {attachments}
      <label htmlFor="chat-message">消息</label>
      <textarea
        id="chat-message"
        value={draft}
        rows={3}
        placeholder="写下你想说的话…"
        aria-describedby="chat-composer-help"
        onChange={(event) => onDraft(event.target.value)}
        onCompositionStart={() => {
          composing.current = true;
        }}
        onCompositionEnd={() => {
          composing.current = false;
        }}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing &&
            !composing.current &&
            event.keyCode !== 229
          ) {
            event.preventDefault();
            if (!disabled && !pending && draft.trim()) onSend();
          }
        }}
      />
      <div className="chat-composer-actions">
        <p id="chat-composer-help" className="metadata">
          文字与所选附件 · Enter 发送 · Shift + Enter 换行
        </p>
        {pending ? (
          <button
            type="button"
            className="button secondary"
            disabled={!canStop || cancelling}
            onClick={onStop}
          >
            <Square size={15} aria-hidden="true" />
            {cancelling ? "正在停止…" : "停止生成"}
          </button>
        ) : (
          <button
            type="submit"
            className="button primary"
            disabled={disabled || !draft.trim()}
          >
            <Send size={16} aria-hidden="true" />
            发送消息
          </button>
        )}
      </div>
      {pending && !canStop && (
        <p className="metadata">
          等待确认生成任务；连接中断时可重新连接后停止。
        </p>
      )}
    </form>
  );
}
