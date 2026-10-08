import { useState } from "react";
import type { LingApi } from "../../api/client";
import { ErrorNotice } from "../../shared/ui";
import { browserApi, exactOrigin } from "./api";
import type { BrowserOrigins } from "./api";
import { useManagementAction } from "../tasks/useManagementAction";

export function BrowserGrants({
  api,
  value,
  refresh,
}: {
  api: LingApi;
  value: BrowserOrigins;
  refresh: () => Promise<void>;
}) {
  const [origin, setOrigin] = useState("");
  const [local, setLocal] = useState(false);
  const [saved, setSaved] = useState(false);
  const action = useManagementAction(refresh);
  return (
    <section className="panel section-panel stack">
      <h2>允许访问的网站</h2>
      <p className="muted small">
        授权只适用于你填写的协议、域名和端口。修改授权会关闭现有浏览器页面，并让待审批操作失效。
      </p>
      {value.grants.length === 0 && (
        <p className="muted small">还没有授权的网站。</p>
      )}
      <ul className="management-list">
        {value.grants.map((grant) => (
          <li key={grant.origin}>
            <div className="section-heading">
              <span className="management-copy">{grant.origin}</span>
              <button
                className="text-button"
                disabled={action.busy}
                aria-label={"撤销授权：" + grant.origin}
                onClick={() =>
                  void action.run(async () => {
                    await browserApi(api, value.agent_id).saveOrigins(
                      value.grants.filter(
                        (item) => item.origin !== grant.origin,
                      ),
                      value.revision,
                    );
                    setSaved(true);
                  })
                }
              >
                撤销授权
              </button>
            </div>
            {grant.allow_local && (
              <p className="metadata">已明确允许本地或局域网访问</p>
            )}
          </li>
        ))}
      </ul>
      <form
        className="management-form"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(async () => {
            const normalized = exactOrigin(origin.trim());
            if (value.grants.some((grant) => grant.origin === normalized))
              throw new Error(
                "这个来源已有授权。若要修改本地访问选项，请先撤销该来源，再重新添加。",
              );
            if (value.grants.length >= 100)
              throw new Error("最多可授权 100 个网站来源。");
            await browserApi(api, value.agent_id).saveOrigins(
              [...value.grants, { origin: normalized, allow_local: local }],
              value.revision,
            );
            setOrigin("");
            setLocal(false);
            setSaved(true);
          });
        }}
      >
        <fieldset disabled={action.busy}>
          <label>
            网站来源
            <input
              required
              maxLength={2048}
              value={origin}
              onChange={(event) => {
                setOrigin(event.target.value);
                setSaved(false);
              }}
              placeholder="https://example.com 或 http://localhost:3000"
            />
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={local}
              onChange={(event) => setLocal(event.target.checked)}
            />
            允许访问我自己的本地项目
          </label>
          <p className="muted small">
            仅当这是你自己的本机或局域网项目时勾选。勾选后，浏览器可以访问该来源对应的本地或私有网络地址；只填写公共网站时无需勾选。
          </p>
          <button className="button secondary">添加网站授权</button>
        </fieldset>
      </form>
      {action.error && <ErrorNotice message={action.error} />}
      {saved && (
        <p className="management-status" role="status">
          浏览器网站授权已更新。
        </p>
      )}
    </section>
  );
}
