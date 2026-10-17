import { useState } from "react";
import type { Conversation } from "../../api/types";
import type { MainConversationState } from "./useContinuation";

function conversationLabel(value: Conversation) {
  return value.title || "未命名会话";
}

/** First-use choice. Existing conversations stay untouched; binding selects the
 * lineage the product treats as the individual's visible main trunk. */
export function MainConversationSetup({
  state,
}: {
  state: MainConversationState;
}) {
  const [choice, setChoice] = useState("");
  const [history, setHistory] = useState(false);
  const value = state.value;
  if (state.status === "loading")
    return <p className="continuity-loading" role="status">正在读取主干…</p>;
  if (state.status === "error")
    return (
      <section className="continuity-setup panel" aria-label="选择主干">
        <h2>暂时无法读取主干</h2>
        <p className="muted">{state.error}</p>
        <button className="button secondary" onClick={() => void state.refresh()}>
          重新读取
        </button>
      </section>
    );
  if (!value || value.conversation) return null;
  const choices = value.choices;
  return (
    <section className="continuity-setup panel" aria-label="选择主干">
      <p className="eyebrow">第一次启用连续对话</p>
      <h2>选择你与灵持续说话的主干</h2>
      <p className="muted">
        灵始终有这一条主干。其他既有会话会完整保留，只在需要时作为历史打开；
        不会自动合并内容，也不会删除消息或附件。
      </p>
      {choices.length > 0 && (
        <div className="continuity-choice-list" role="radiogroup" aria-label="已有会话">
          {choices.slice(0, history ? choices.length : 5).map((item) => (
            <label className="continuity-choice" key={item.id}>
              <input
                type="radio"
                name="main-conversation"
                value={item.id}
                checked={choice === item.id}
                onChange={() => setChoice(item.id)}
              />
              <span>
                <strong>{conversationLabel(item)}</strong>
                <small className="metadata">{item.id}</small>
              </span>
            </label>
          ))}
        </div>
      )}
      {choices.length > 5 && (
        <button className="text-button" type="button" onClick={() => setHistory(!history)}>
          {history ? "收起较早会话" : `查看其余 ${choices.length - 5} 个会话`}
        </button>
      )}
      {choices.length === 0 && (
        <p className="notice">还没有会话。选择“建立新主干”后，第一条消息会开始这段连续对话。</p>
      )}
      {state.error && <><p className="notice error" role="alert">{state.error}</p><button className="text-button" onClick={() => void state.refresh()}>读取最新选择</button></>}
      <div className="button-row">
        <button
          className="button primary"
          disabled={state.busy || (choices.length > 0 && !choice)}
          onClick={() => void state.bind(choice || null)}
        >
          {state.busy ? "正在保存…" : choice ? "使用所选会话作为主干" : "建立新主干"}
        </button>
        {choices.length > 0 && <button className="button secondary" disabled={state.busy} onClick={() => void state.bind(null)}>建立新主干</button>}
      </div>
    </section>
  );
}

export function MainConversationMenu({
  state,
  openHistory,
  openWork,
}: {
  state: MainConversationState;
  openHistory: () => void;
  openWork: () => void;
}) {
  const [choosing, setChoosing] = useState(false);
  const value = state.value;
  if (!value?.conversation) return null;
  return (
    <div className="continuity-main-actions">
      <button className="button secondary small-button" type="button" onClick={openWork}>
        工作与经历
      </button>
      <button className="text-button" type="button" onClick={openHistory}>
        历史与旧会话
      </button>
      <button className="text-button" type="button" onClick={() => setChoosing(!choosing)}>
        更换主干
      </button>
      {choosing && (
        <div className="continuity-rebind" role="group" aria-label="更换主干">
          <p className="metadata">只改变日常入口；原会话和全部历史保留。</p>
          {value.choices.map((item) => (
            <button
              key={item.id}
              className="text-button"
              type="button"
              disabled={state.busy || item.id === value.conversation?.id}
              onClick={() => void state.bind(item.id).then((saved) => { if (saved) setChoosing(false); })}
            >
              {conversationLabel(item)}
            </button>
          ))}
          {state.error && <><p className="notice error" role="alert">{state.error}</p><button className="text-button" onClick={() => void state.refresh()}>读取最新选择</button></>}
        </div>
      )}
    </div>
  );
}
