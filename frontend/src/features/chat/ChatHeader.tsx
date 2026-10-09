import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";

/** One quiet row: the conversation name, and everything occasional behind the ⋯. */
export function ChatHeader({
  title,
  selecting,
  canSelect,
  onSelectTurns,
  segmentsOpen,
  onToggleSegments,
  onOpenSettings,
  disabled,
}: {
  title: string;
  selecting: boolean;
  canSelect: boolean;
  onSelectTurns: () => void;
  segmentsOpen: boolean;
  onToggleSegments: () => void;
  onOpenSettings: () => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (box.current && !box.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <header className="chat-head">
      <div className="chat-head-inner">
        <h1 className="chat-head-title" tabIndex={-1}>
          {title}
        </h1>
        <div className="chat-head-menu" ref={box}>
        <button
          type="button"
          className="icon-button"
          aria-label="对话选项"
          aria-haspopup="true"
          aria-expanded={open}
          disabled={disabled}
          onClick={() => setOpen(!open)}
        >
          <MoreHorizontal size={17} aria-hidden="true" />
        </button>
        {open && (
          <div className="chat-head-popup">
            <button
              type="button"
              className="chat-head-action"
              disabled={!canSelect}
              onClick={() => {
                onSelectTurns();
                setOpen(false);
              }}
            >
              {selecting ? "退出批量删除" : "批量删除"}
            </button>
            <button
              type="button"
              className="chat-head-action"
              onClick={() => {
                onToggleSegments();
                setOpen(false);
              }}
            >
              {segmentsOpen ? "收起交流主题与分段" : "交流主题与分段"}
            </button>
            <button
              type="button"
              className="chat-head-action"
              onClick={() => {
                onOpenSettings();
                setOpen(false);
              }}
            >
              模型设置
            </button>
            <p className="metadata chat-head-note">
              切页会断开连接，生成会继续。返回后自动接续。
            </p>
          </div>
        )}
        </div>
      </div>
    </header>
  );
}
