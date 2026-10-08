import { useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { ModelSelection, SavedModel } from "../../api/types";
import { ErrorNotice } from "../../shared/ui";

export function ModelSelectionForm({
  api,
  profiles,
  selection,
  saved,
}: {
  api: LingApi;
  profiles: SavedModel[];
  selection: ModelSelection;
  saved: () => Promise<void>;
}) {
  const [primary, setPrimary] = useState(selection.primary ?? "");
  const [fallback, setFallback] = useState(selection.fallback ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <form
      className="model-selection"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || !primary) return;
        setBusy(true);
        setError("");
        try {
          await api.selectModels(primary, fallback || null);
          await saved();
        } catch (error) {
          setError(errorMessage(error));
        } finally {
          setBusy(false);
        }
      }}
    >
      <h3>主用与备用</h3>
      <p className="small muted">
        保存连接后，再明确选择要使用的模型。此操作只保存选择，不会发起模型调用。
      </p>
      <div className="model-fields">
        <label>
          主用模型
          <select
            value={primary}
            disabled={busy}
            onChange={(event) => {
              setPrimary(event.target.value);
              if (fallback === event.target.value) setFallback("");
            }}
          >
            <option value="">尚未选择</option>
            {profiles.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          备用模型
          <select
            value={fallback}
            disabled={busy}
            onChange={(event) => setFallback(event.target.value)}
          >
            <option value="">不使用备用</option>
            {profiles
              .filter((profile) => profile.id !== primary)
              .map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name}
                </option>
              ))}
          </select>
        </label>
      </div>
      {error && <ErrorNotice message={error} />}
      <button className="button secondary" disabled={busy || !primary}>
        {busy ? "正在保存…" : "保存模型选择"}
      </button>
    </form>
  );
}
