import { useState } from "react";
import { Pause, Play } from "lucide-react";

/**
 * Live controls for a running chat loop. Pause and resume act at the next step
 * boundary (a running tool is never killed); a guide is injected as a user turn
 * before the next step. Nothing here is shown unless the loop is actually live.
 */
export function ChatRunControls({
  paused,
  onPause,
  onResume,
  onGuide,
  disabled,
}: {
  paused: boolean;
  onPause: () => void;
  onResume: () => void;
  onGuide: (text: string) => Promise<boolean>;
  disabled: boolean;
}) {
  const [text, setText] = useState("");
  const submit = async () => {
    if (!text.trim()) return;
    if (await onGuide(text)) setText("");
  };
  return (
    <div className="chat-run-controls" role="group" aria-label="生成控制">
      <button
        type="button"
        className="button secondary chat-run-toggle"
        disabled={disabled}
        onClick={paused ? onResume : onPause}
      >
        {paused ? (
          <>
            <Play size={14} aria-hidden="true" /> 继续
          </>
        ) : (
          <>
            <Pause size={14} aria-hidden="true" /> 暂停
          </>
        )}
      </button>
      <form
        className="chat-guide"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <input
          aria-label="中途指导"
          placeholder={
            paused
              ? "写下指导，点继续时会在下一步生效…"
              : "给正在进行的生成一句指导…"
          }
          value={text}
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
        />
        <button
          type="submit"
          className="text-button"
          disabled={disabled || !text.trim()}
        >
          提交指导
        </button>
      </form>
    </div>
  );
}
