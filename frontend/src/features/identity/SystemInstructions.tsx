import { useState } from "react";
import { RotateCcw } from "lucide-react";
import type { Instructions } from "../../api/types";

/** -1 means "adding a new rule", a number means "editing that rule", null means idle. */
type Editing = number | null;

export function SystemInstructions({
  agentName,
  instructions,
  busy,
  save,
  reset,
}: {
  agentName: string;
  instructions: Instructions;
  busy: boolean;
  save: (rules: string[]) => Promise<void>;
  reset: () => Promise<void>;
}) {
  const [editing, setEditing] = useState<Editing>(null);
  const [text, setText] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);

  function startEdit(target: number) {
    setEditing(target);
    setText(target < 0 ? "" : instructions.rules[target]);
  }

  function cancel() {
    setEditing(null);
    setText("");
  }

  async function commit() {
    const value = text.trim();
    if (!value || editing === null) return;
    const next = [...instructions.rules];
    if (editing < 0) next.push(value);
    else next[editing] = value;
    await save(next);
    cancel();
  }

  const editor = (
    <div className="rule-editor">
      <label>
        指令内容
        <textarea
          aria-label="指令内容"
          rows={2}
          maxLength={1000}
          value={text}
          disabled={busy}
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      <div className="button-row">
        <button
          className="button secondary small-button"
          disabled={busy}
          onClick={cancel}
        >
          取消
        </button>
        <button
          className="button primary small-button"
          disabled={busy || !text.trim()}
          onClick={() => void commit()}
        >
          保存这条
        </button>
      </div>
    </div>
  );

  return (
    <section className="panel section-panel" aria-labelledby="instructions-title">
      <div className="section-heading">
        <h2 id="instructions-title">系统指令</h2>
        <span className="metadata">每条都能看、能改、能删</span>
      </div>
      <p className="muted small">
        这是每次对话开头交给{agentName}的规矩。原先藏在程序里的那几条也搬到了这里。
      </p>

      {instructions.rules.length === 0 ? (
        <p className="inline-warning">
          现在一条指令都没有，{agentName}只会收到名称和当前状态。
        </p>
      ) : (
        <ul className="rule-list">
          {instructions.rules.map((rule, index) => (
            <li key={index}>
              {editing === index ? (
                editor
              ) : (
                <>
                  <p className="rule-text">{rule}</p>
                  <div className="row-actions">
                    <button
                      className="button secondary small-button"
                      disabled={busy || editing !== null}
                      aria-label={"修改第 " + (index + 1) + " 条指令"}
                      onClick={() => startEdit(index)}
                    >
                      改
                    </button>
                    <button
                      className="button secondary small-button"
                      disabled={busy || editing !== null}
                      aria-label={"删除第 " + (index + 1) + " 条指令"}
                      onClick={() =>
                        void save(instructions.rules.filter((_, at) => at !== index))
                      }
                    >
                      删
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {editing === -1 ? (
        editor
      ) : (
        <div className="button-row">
          <button
            className="button secondary"
            disabled={busy || editing !== null}
            onClick={() => startEdit(-1)}
          >
            + 添加一条指令
          </button>
        </div>
      )}

      {instructions.customized && (
        <details className="handled">
          <summary>
            程序自带的 {instructions.defaults.length} 条（内容参考，不生效中）
          </summary>
          <ul className="rule-list plain">
            {instructions.defaults.map((rule) => (
              <li key={rule}>
                <p className="rule-text">{rule}</p>
              </li>
            ))}
          </ul>
          {confirmReset ? (
            <div className="button-row">
              <button
                className="button secondary small-button"
                disabled={busy}
                onClick={() => setConfirmReset(false)}
              >
                取消
              </button>
              <button
                className="button primary small-button"
                disabled={busy}
                onClick={() => {
                  setConfirmReset(false);
                  void reset();
                }}
              >
                确认恢复
              </button>
            </div>
          ) : (
            <div className="button-row">
              <button
                className="button secondary small-button"
                disabled={busy}
                onClick={() => setConfirmReset(true)}
              >
                <RotateCcw size={14} aria-hidden="true" />
                恢复成这几条
              </button>
            </div>
          )}
        </details>
      )}

      <p className="field-hint">
        权限不靠这里管：写「你可以读写某个盘」不会真的开权限，那要去「设置 · 权限」改。
      </p>
    </section>
  );
}
