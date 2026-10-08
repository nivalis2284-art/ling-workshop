import { useState } from "react";
import type { LingApi } from "../../api/client";
import { ErrorNotice } from "../../shared/ui";
import { useManagementAction } from "../tasks/useManagementAction";
import { browserApi, loginUrl } from "./api";
import type { BrowserState } from "./api";

export function BrowserLogin({
  api,
  state,
  refresh,
}: {
  api: LingApi;
  state: BrowserState;
  refresh: () => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const [message, setMessage] = useState("");
  const action = useManagementAction(refresh);
  const manual = state.page?.manual_login === true;
  return (
    <section className="panel section-panel stack">
      <h2>手动登录网站</h2>
      <p className="muted small">
        由你在打开的浏览器窗口中登录。登录期间自动操作暂停；完成后点击下方按钮结束手动登录。
      </p>
      <p>
        {manual
          ? "正在等待你完成手动登录"
          : state.page?.url
            ? "当前页面：" + state.page.url
            : "当前没有打开的浏览器页面"}
      </p>
      {manual ? (
        <button
          className="button primary"
          disabled={action.busy}
          onClick={() =>
            void action.run(async () => {
              await browserApi(api, state.agent_id).finishLogin();
              setMessage("手动登录已结束。");
            })
          }
        >
          我已完成登录
        </button>
      ) : (
        <form
          className="management-form"
          onSubmit={(event) => {
            event.preventDefault();
            void action.run(async () => {
              const value = loginUrl(url.trim(), state.origins);
              await browserApi(api, state.agent_id).login(
                value,
                state.origins.revision,
              );
              setMessage("已打开浏览器，请在窗口中完成登录。");
            });
          }}
        >
          <label>
            登录页面地址
            <input
              type="url"
              required
              maxLength={8192}
              value={url}
              disabled={action.busy}
              onChange={(event) => {
                setUrl(event.target.value);
                setMessage("");
              }}
              placeholder="https://example.com/login"
            />
          </label>
          <button className="button secondary" disabled={action.busy}>
            打开浏览器手动登录
          </button>
        </form>
      )}
      {action.error && <ErrorNotice message={action.error} />}
      {message && (
        <p className="management-status" role="status">
          {message}
        </p>
      )}
    </section>
  );
}
