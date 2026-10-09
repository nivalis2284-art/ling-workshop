import { useState } from "react";
import {
  entryChoices,
  entryKeyFor,
  identityEntryLabel,
} from "../../shared/identityFormatting";

/** Owner-facing editor for one entry. A fixed key means "change the text only". */
export function EntryForm({
  kind,
  fixedKey,
  initialText = "",
  busy,
  onCancel,
  submit,
}: {
  kind: string;
  fixedKey?: string;
  initialText?: string;
  busy: boolean;
  onCancel: () => void;
  submit: (key: string, text: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [body, setBody] = useState(initialText);
  const [error, setError] = useState("");

  const declared = fixedKey ? entryChoices(fixedKey) : null;
  const choices =
    declared && body && !declared.some((item) => item.value === body)
      ? [{ value: body, label: body + "（当前值）" }, ...declared]
      : declared;

  async function commit() {
    if (!body.trim()) {
      setError("内容不能为空。");
      return;
    }
    if (!fixedKey && !name.trim()) {
      setError("给这条起个名字，以后好找。");
      return;
    }
    setError("");
    await submit(fixedKey ?? entryKeyFor(kind, name), body.trim());
  }

  return (
    <div className="entry-editor">
      {!fixedKey && (
        <label>
          这条叫什么
          <input
            aria-label="条目名字"
            maxLength={60}
            placeholder="例如：对我的称呼习惯"
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      )}
      <label>
        {fixedKey ? "「" + identityEntryLabel(fixedKey) + "」的内容" : "内容"}
        {choices ? (
          <select
            aria-label="内容"
            value={body}
            disabled={busy}
            onChange={(event) => setBody(event.target.value)}
          >
            {choices.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        ) : (
          <textarea
            aria-label="内容"
            rows={3}
            maxLength={6000}
            placeholder="用一句话写下这项认识"
            value={body}
            disabled={busy}
            onChange={(event) => setBody(event.target.value)}
          />
        )}
      </label>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      <div className="button-row">
        <button
          className="button secondary small-button"
          disabled={busy}
          onClick={onCancel}
        >
          取消
        </button>
        <button
          className="button primary small-button"
          disabled={busy}
          onClick={() => void commit()}
        >
          保存
        </button>
      </div>
    </div>
  );
}
