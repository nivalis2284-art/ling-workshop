import { useRef, useState } from "react";
import { Pause, Play } from "lucide-react";

/**
 * Live controls for a running chat loop. Pause and resume act at the next step
 * boundary (a running tool is never killed); a guide is injected as a user turn
 * before the next step. Nothing here is shown unless the loop is actually live.
 */
export function ChatRunControls({
  paused,
  pausePending,
  resumePending,
  onPause,
  onResume,
  onGuide,
  disabled,
}: {
  paused: boolean;
  // 暂停/继续请求已发出但后端事件未到：界面显示等待中并挡住重复点击，
  // 因为 POST 只置标志，真正生效要看后端补发的 paused/resumed 事件。
  pausePending?: boolean;
  resumePending?: boolean;
  onPause: () => void;
  onResume: () => void;
  onGuide: (text: string) => Promise<boolean>;
  disabled: boolean;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const inFlight = useRef(false);
  const submit = async () => {
    // 连按回车或上一次还在路上时直接忽略：同一条指导注入两次没有意义。
    if (inFlight.current || !text.trim()) return;
    inFlight.current = true;
    setSending(true);
    try {
      if (await onGuide(text)) setText("");
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };
  const waiting = !!pausePending || !!resumePending;
  return (
    <div className="chat-run-controls" role="group" aria-label="生成控制">
      <button
        type="button"
        className="button secondary chat-run-toggle"
        disabled={disabled || waiting}
        onClick={paused ? onResume : onPause}
      >
        {pausePending ? (
          <>
            <Pause size={14} aria-hidden="true" /> 暂停中…
          </>
        ) : resumePending ? (
          <>
            <Play size={14} aria-hidden="true" /> 继续中…
          </>
        ) : paused ? (
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
          disabled={disabled || sending || !text.trim()}
        >
          提交指导
        </button>
      </form>
    </div>
  );
}
