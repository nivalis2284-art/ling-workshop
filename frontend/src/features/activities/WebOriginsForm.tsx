import { useEffect, useState } from "react";
import type { LingApi } from "../../api/client";
import { ErrorNotice } from "../../shared/ui";
import { activitiesApi, parseOrigins } from "./api";
import type { WebOrigins } from "./api";
import { useManagementAction } from "../tasks/useManagementAction";

export function WebOriginsForm({
  api,
  value,
  refresh,
}: {
  api: LingApi;
  value: WebOrigins;
  refresh: () => Promise<void>;
}) {
  const [text, setText] = useState(value.origins.join("\n"));
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  const action = useManagementAction(refresh);
  useEffect(() => {
    if (!dirty) setText(value.origins.join("\n"));
  }, [value, dirty]);
  return (
    <section className="panel section-panel">
      <h2>网页关注授权</h2>
      <p className="muted small">
        先授权具体网站，再创建网页关注。每行一个来源，只包含协议、域名和端口。移除来源会暂停相关关注。
      </p>
      <form
        className="management-form"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(async () => {
            await activitiesApi(api).saveOrigins(
              value.agent_id,
              parseOrigins(text),
              value.revision,
            );
            setDirty(false);
            setSaved(true);
          });
        }}
      >
        <label>
          允许关注的网站来源
          <textarea
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setDirty(true);
              setSaved(false);
            }}
            placeholder="https://example.com"
            disabled={action.busy}
          />
        </label>
        <button className="button secondary" disabled={action.busy}>
          保存网页关注授权
        </button>
        {action.error && <ErrorNotice message={action.error} />}
        {saved && (
          <p role="status" className="management-status">
            网页关注授权已保存。
          </p>
        )}
      </form>
    </section>
  );
}
