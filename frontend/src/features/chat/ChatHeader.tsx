import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";
import type { ConversationBranch } from "../../api/types";

/** One quiet row: the conversation name, and everything occasional behind the ⋯. */
export function ChatHeader({
  title,
  branches,
  onBranch,
  segmentsOpen,
  onToggleSegments,
  onOpenSettings,
  disabled,
}: {
  title: string;
  branches: ConversationBranch[];
  onBranch: (id: string) => void;
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
            {branches.length > 0 && (
              <label className="chat-head-field">
                对话分支
                <select
                  value={branches.find((branch) => branch.selected)?.id ?? ""}
                  disabled={disabled}
                  onChange={(event) => onBranch(event.target.value)}
                >
                  {!branches.some((branch) => branch.selected) && (
                    <option value="" disabled>
                      选择分支
                    </option>
                  )}
                  {branches.map((branch, index) => (
                    <option key={branch.id} value={branch.id}>
                      {branch.selected ? "当前 · " : ""}分支 {index + 1} ·{" "}
                      {branch.text.slice(0, 30)}
                    </option>
                  ))}
                </select>
              </label>
            )}
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
    </header>
  );
}
