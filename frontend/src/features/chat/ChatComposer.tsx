import { useRef } from "react";
import type { ReactNode } from "react";
import { ArrowUp, LoaderCircle, Plus, Square } from "lucide-react";

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
  attachmentsOpen,
  onToggleAttachments,
  controls,
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
  attachmentsOpen: boolean;
  onToggleAttachments: () => void;
  controls?: ReactNode;
}) {
  const composing = useRef(false);
  const ready = !disabled && !pending && !!draft.trim();
  return (
    <form
      className="chat-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) onSend();
      }}
    >
      {attachments}
      <div className="chat-composer-box">
        <textarea
          aria-label="消息"
          value={draft}
          rows={2}
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
              if (ready) onSend();
            }
          }}
        />
        <div className="chat-composer-row">
          <button
            type="button"
            className={"chat-plus" + (attachmentsOpen ? " active" : "")}
            aria-label="添加附件"
            aria-expanded={attachmentsOpen}
            onClick={onToggleAttachments}
          >
            <Plus size={18} aria-hidden="true" />
          </button>
          <div className="chat-composer-buttons">
            {controls}
            {pending ? (
              <button
                type="button"
                className="chat-send stop"
                aria-label="停止生成"
                title="停止生成"
                disabled={!canStop || cancelling}
                onClick={onStop}
              >
                {cancelling ? (
                  <LoaderCircle size={17} className="spin" aria-hidden="true" />
                ) : (
                  <Square size={15} aria-hidden="true" />
                )}
              </button>
            ) : (
              <button
                type="submit"
                className="chat-send"
                aria-label="发送消息"
                title="发送消息"
                disabled={!ready}
              >
                <ArrowUp size={18} aria-hidden="true" />
              </button>
            )}
          </div>
        </div>
      </div>
      <p id="chat-composer-help" className="chat-composer-help">
        文字与所选附件 · Enter 发送 · Shift + Enter 换行
      </p>
      {pending && !canStop && (
        <p className="chat-composer-help">
          等待确认生成任务；连接中断时可重新连接后停止。
        </p>
      )}
    </form>
  );
}
