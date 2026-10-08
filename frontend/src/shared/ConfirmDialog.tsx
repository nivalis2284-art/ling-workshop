import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    cancel.current?.focus();
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        ref={ref}
        className="modal panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        aria-describedby={id + "-body"}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) {
            event.stopPropagation();
            onCancel();
          }
          if (event.key !== "Tab") return;
          const focusable = ref.current?.querySelectorAll<HTMLElement>(
            'button:not(:disabled), [href], input:not(:disabled), [tabindex="0"]',
          );
          if (!focusable?.length) {
            event.preventDefault();
            return;
          }
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          }
          if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
      >
        <p className="eyebrow">请确认这次操作</p>
        <h2 id={id}>{title}</h2>
        <div id={id + "-body"} className="modal-copy">
          {children}
        </div>
        <div className="button-row">
          <button
            ref={cancel}
            className="button secondary"
            disabled={busy}
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className="button primary"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "正在处理…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
