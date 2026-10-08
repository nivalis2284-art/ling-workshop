import { useCallback, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { LingApi } from "../../api/client";
import type { SavedModel } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";
import { ModelEditor } from "./ModelEditor";
import { ModelSelectionForm } from "./ModelSelectionForm";

export function ModelSettings({ api }: { api: LingApi }) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const [profiles, selection] = await Promise.all([
        api.models(signal),
        api.modelSelection(signal),
      ]);
      return { profiles, selection };
    },
    [api],
  );
  const resource = useResource(load);
  const [editing, setEditing] = useState<SavedModel | undefined>();
  const [message, setMessage] = useState("");
  return (
    <section className="panel section-panel" aria-labelledby="models-title">
      <div className="section-heading">
        <h2 id="models-title">模型连接</h2>
        <button
          className="icon-button"
          aria-label="刷新模型配置"
          disabled={resource.status === "loading"}
          onClick={() => void resource.refresh()}
        >
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>
      <p className="muted">
        配置会保存在本地，密钥仅保留在服务的本次运行中。聊天界面仍在开发；凭据已提供不代表连接已验证。
      </p>
      {message && (
        <p className="notice success" role="status">
          {message}
        </p>
      )}
      {resource.status === "loading" && <Loading label="正在读取模型配置…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          {resource.data.profiles.length === 0 ? (
            <p className="small muted">
              还没有模型连接。添加并选择之前，不会发起模型调用。
            </p>
          ) : (
            <ul className="model-list">
              {resource.data.profiles.map((profile) => (
                <li key={profile.id}>
                  <div>
                    <strong>{profile.name}</strong>
                    <p>
                      {profile.profile.model} ·{" "}
                      {profile.credentials_available
                        ? "本次运行已提供凭据"
                        : "本次运行未提供凭据"}
                    </p>
                  </div>
                  <button
                    className="button secondary small-button"
                    onClick={() => {
                      setEditing(profile);
                      setMessage("");
                    }}
                    aria-label={"编辑连接 " + profile.name}
                  >
                    编辑
                  </button>
                </li>
              ))}
            </ul>
          )}
          <ModelSelectionForm
            api={api}
            profiles={resource.data.profiles}
            selection={resource.data.selection}
            saved={async () => {
              setMessage("模型选择已保存，尚未发起模型调用。");
              await resource.refresh();
            }}
          />
          {editing && (
            <button
              className="text-button"
              onClick={() => setEditing(undefined)}
            >
              改为添加新连接
            </button>
          )}
          <ModelEditor
            key={editing?.id ?? "new"}
            api={api}
            existing={editing}
            saved={async () => {
              setEditing(undefined);
              setMessage(
                "模型配置已保存，密钥输入已清空。请按需选择主用与备用模型。",
              );
              await resource.refresh();
            }}
          />
        </>
      )}
    </section>
  );
}
