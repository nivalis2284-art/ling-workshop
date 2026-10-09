import { useEffect, useRef, useState } from "react";
import { Check, ChevronUp, RefreshCw } from "lucide-react";
import type { SavedModel } from "../../api/types";

/** Compact model + thinking-effort picker that sits at the composer's bottom-right. */
export function ChatControls({
  profiles,
  profileId,
  onProfile,
  effortLevels,
  effort,
  onEffort,
  disabled,
  locked,
  loading,
  onRefresh,
}: {
  profiles: SavedModel[];
  profileId: string;
  onProfile: (id: string) => void;
  effortLevels: string[];
  effort: string;
  onEffort: (level: string) => void;
  disabled: boolean;
  locked: boolean;
  loading: boolean;
  onRefresh: () => void;
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

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const current = profiles.find((item) => item.id === profileId);
  return (
    <div className="chat-controls" ref={box}>
      {open && (
        <div className="chat-controls-popup">
          <div className="chat-controls-head">
            <span>选择模型</span>
            <button
              type="button"
              className="icon-button"
              aria-label="刷新模型列表"
              disabled={loading}
              onClick={onRefresh}
            >
              <RefreshCw size={14} aria-hidden="true" />
            </button>
          </div>
          {profiles.length === 0 ? (
            <p className="metadata">还没有可用的模型连接。</p>
          ) : (
            <ul className="chat-model-options" aria-label="选择模型">
              {profiles.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={item.id === profileId}
                    className={
                      "chat-option" + (item.id === profileId ? " current" : "")
                    }
                    onClick={() => {
                      onProfile(item.id);
                      setOpen(false);
                    }}
                  >
                    <span>{item.name}</span>
                    {item.id === profileId && (
                      <Check size={15} aria-hidden="true" />
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {!!effortLevels.length && (
            <>
              <p className="chat-controls-head plain">思考强度</p>
              <div className="chat-effort-options" aria-label="思考强度">
                {["", ...effortLevels].map((level) => (
                  <button
                    key={level || "default"}
                    type="button"
                    role="radio"
                    aria-checked={effort === level}
                    disabled={locked}
                    className={
                      "chat-option compact" + (effort === level ? " current" : "")
                    }
                    onClick={() => onEffort(level)}
                  >
                    {level || "模型默认"}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      )}
      <button
        type="button"
        className="chat-controls-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        aria-label="对话模型与思考强度"
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        <span className="chat-controls-model">
          {current ? current.name : "选择模型"}
        </span>
        {!!effortLevels.length && (
          <span className="chat-controls-effort">{effort || "模型默认"}</span>
        )}
        <ChevronUp size={15} aria-hidden="true" />
      </button>
    </div>
  );
}
