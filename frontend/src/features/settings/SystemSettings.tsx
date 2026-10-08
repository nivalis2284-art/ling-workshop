import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice, Loading } from "../../shared/ui";
import { useResource } from "../../shared/useResource";

type Backups = {
  enabled: boolean;
  path: string;
  retention: number;
  last_success_at: string | null;
  last_backup: string | null;
  last_error: string | null;
};
type Startup = { available: boolean; enabled: boolean };
type Restore = {
  status: "idle" | "queued";
  plan_id?: string;
  quit_available: boolean;
};
type Plan = {
  plan_id: string;
  summary: { agents: number; sources: number; attachments: number };
};
type Settings = { backups: Backups; startup: Startup; restore: Restore };

function SystemEditor({ api, initial }: { api: LingApi; initial: Settings }) {
  const [backups, setBackups] = useState(initial.backups);
  const [enabled, setEnabled] = useState(backups.enabled);
  const [path, setPath] = useState(backups.path);
  const [retention, setRetention] = useState(String(backups.retention));
  const [startup, setStartup] = useState(initial.startup);
  const [login, setLogin] = useState(startup.enabled);
  const [restore, setRestore] = useState(initial.restore);
  const [format, setFormat] = useState("backup");
  const [source, setSource] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [quitting, setQuitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function act(operation: (signal: AbortSignal) => Promise<void>) {
    if (busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const queued = restore.status === "queued";
  return (
    <>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void act(async (signal) => {
            const result = await api.call<Backups>("/system/backups", {
              method: "PUT",
              signal,
              body: JSON.stringify({
                enabled,
                path: path.trim(),
                retention: Number(retention),
              }),
            });
            if (signal.aborted) return;
            setBackups(result);
            setPath(result.path);
            setRetention(String(result.retention));
            setEnabled(result.enabled);
            setNotice("自动备份设置已保存。下次空闲时按每日计划执行。");
          });
        }}
      >
        <h3>每日自动备份</h3>
        <p className="muted">每天在本地空闲时保存所有灵的数据库和附件。</p>
        <label>
          <input
            type="checkbox"
            checked={enabled}
            disabled={busy}
            onChange={(event) => {
              setEnabled(event.target.checked);
              setNotice("");
            }}
          />
          启用每日自动备份
        </label>
        <label htmlFor="automatic-backup-path">自动备份目录</label>
        <input
          id="automatic-backup-path"
          value={path}
          required
          disabled={busy}
          autoComplete="off"
          onChange={(event) => {
            setPath(event.target.value);
            setNotice("");
          }}
        />
        <label htmlFor="automatic-backup-retention">保留成功备份份数</label>
        <input
          id="automatic-backup-retention"
          type="number"
          min={1}
          max={365}
          step={1}
          required
          value={retention}
          disabled={busy}
          onChange={(event) => {
            setRetention(event.target.value);
            setNotice("");
          }}
        />
        <p className="field-help">
          默认保留 14 份。只清理本功能创建且通过完整校验的旧备份。
        </p>
        {backups.last_success_at ? (
          <p className="metadata break-anywhere">
            最近成功备份：{new Date(backups.last_success_at).toLocaleString()}
            <br />
            {backups.last_backup}
          </p>
        ) : (
          <p className="metadata">
            尚无成功的自动备份记录。每天在空闲时尝试一次。
          </p>
        )}
        {backups.last_error && <ErrorNotice message={backups.last_error} />}
        <div className="button-row">
          <button className="button secondary" disabled={busy}>
            保存自动备份设置
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={busy}
            onClick={() =>
              void act(async (signal) => {
                const result = await api.call<Backups>("/system/backups", {
                  signal,
                });
                if (!signal.aborted) setBackups(result);
              })
            }
          >
            刷新备份记录
          </button>
        </div>
      </form>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void act(async (signal) => {
            const result = await api.call<Startup>("/system/startup", {
              method: "PUT",
              signal,
              body: JSON.stringify({ enabled: login }),
            });
            if (signal.aborted) return;
            setStartup(result);
            setLogin(result.enabled);
            setNotice(result.enabled ? "登录启动已启用。" : "登录启动已关闭。");
          });
        }}
      >
        <h3>登录启动</h3>
        <label>
          <input
            type="checkbox"
            checked={login}
            disabled={busy || !startup.available}
            onChange={(event) => {
              setLogin(event.target.checked);
              setNotice("");
            }}
          />
          登录 Windows 时启动 LingNexus
        </label>
        <p className="field-help">
          仅作用于当前 Windows 用户。登录后在托盘运行。
        </p>
        {!startup.available && (
          <p className="muted">当前环境不支持 Windows 登录启动。</p>
        )}
        <button
          className="button secondary"
          disabled={busy || !startup.available}
        >
          保存登录启动设置
        </button>
      </form>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setPlan(null);
          void act(async (signal) => {
            const result = await api.call<Plan>("/system/restore/prepare", {
              method: "POST",
              signal,
              body: JSON.stringify({
                source_format: format,
                source_path: source.trim(),
              }),
            });
            if (!signal.aborted) setPlan(result);
          });
        }}
      >
        <h3>恢复备份或导出资料</h3>
        <p className="muted">
          先验证并预览，再确认排队。下次桌面启动时应用，并保留恢复前的回滚备份。
        </p>
        <label htmlFor="restore-format">恢复来源格式</label>
        <select
          id="restore-format"
          value={format}
          disabled={busy || queued}
          onChange={(event) => {
            setFormat(event.target.value);
            setPlan(null);
            setNotice("");
          }}
        >
          <option value="backup">数据库备份（.db 和 .assets 文件夹）</option>
          <option value="export">导出资料文件夹</option>
        </select>
        <label htmlFor="restore-source">恢复来源路径</label>
        <input
          id="restore-source"
          required
          value={source}
          disabled={busy || queued}
          autoComplete="off"
          onChange={(event) => {
            setSource(event.target.value);
            setPlan(null);
            setNotice("");
          }}
        />
        <button
          className="button secondary"
          disabled={busy || queued || !source.trim()}
        >
          验证并预览恢复
        </button>
        {plan && !queued && (
          <div className="notice">
            <p>
              {plan.summary.agents} 位灵 · {plan.summary.sources} 条来源 ·{" "}
              {plan.summary.attachments} 个附件
            </p>
            <p className="path-preview">{source}</p>
            <button
              type="button"
              className="button secondary"
              disabled={busy}
              onClick={() => setConfirm(true)}
            >
              排队恢复
            </button>
          </div>
        )}
      </form>
      {queued && (
        <div className="notice" role="status">
          <p>
            恢复已排队。请完全退出 LingNexus
            后重新打开，恢复将在下次桌面启动时应用。
          </p>
          <p>
            关闭浏览器窗口不会退出托盘中的 LingNexus。当前会话仍使用原资料库。
          </p>
          {restore.quit_available && (
            <button
              className="button secondary"
              disabled={busy || quitting}
              onClick={() =>
                void act(async (signal) => {
                  const result = await api.call<{ quit_requested: boolean }>(
                    "/system/restore/quit",
                    {
                      method: "POST",
                      signal,
                      body: JSON.stringify({
                        plan_id: restore.plan_id,
                        confirmed: true,
                      }),
                    },
                  );
                  if (signal.aborted) return;
                  setQuitting(result.quit_requested);
                  setNotice(
                    result.quit_requested
                      ? "正在退出，请稍后从桌面入口重新打开 LingNexus。"
                      : "请从托盘完全退出后重新打开 LingNexus。",
                  );
                })
              }
            >
              退出 LingNexus，稍后重新打开
            </button>
          )}
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
      {confirm && plan && (
        <ConfirmDialog
          title="确认排队恢复"
          confirmLabel="确认在下次启动恢复"
          busy={busy}
          onCancel={() => setConfirm(false)}
          onConfirm={() => {
            void act(async (signal) => {
              try {
                const result = await api.call<Restore>(
                  "/system/restore/commit",
                  {
                    method: "POST",
                    signal,
                    body: JSON.stringify({
                      plan_id: plan.plan_id,
                      confirmed: true,
                    }),
                  },
                );
                if (!signal.aborted) {
                  setRestore(result);
                  setPlan(null);
                }
              } catch (error) {
                if (!signal.aborted) setPlan(null);
                throw error;
              } finally {
                if (!signal.aborted) setConfirm(false);
              }
            });
          }}
        >
          <p>
            下次桌面启动时，将用此资料替换所有灵的当前资料库。恢复前会保存当前数据库和附件，供回滚使用。
          </p>
          <p className="path-preview">{source}</p>
          <p>确认后仍需完全退出并重新打开 LingNexus。</p>
        </ConfirmDialog>
      )}
    </>
  );
}

export function SystemSettings({ api }: { api: LingApi }) {
  const load = useCallback(
    async (signal: AbortSignal): Promise<Settings> => {
      const [backups, startup, restore] = await Promise.all([
        api.call<Backups>("/system/backups", { signal }),
        api.call<Startup>("/system/startup", { signal }),
        api.call<Restore>("/system/restore", { signal }),
      ]);
      return { backups, startup, restore };
    },
    [api],
  );
  const resource = useResource(load);
  return (
    <section
      className="panel section-panel"
      aria-labelledby="system-settings-heading"
    >
      <div className="section-heading">
        <h2 id="system-settings-heading">备份、恢复与启动</h2>
      </div>
      {resource.status === "loading" && <Loading label="正在读取系统设置…" />}
      {resource.status === "error" && (
        <div className="notice error" role="status">
          <p>{resource.error}</p>
          <button
            className="text-button"
            onClick={() => void resource.refresh()}
          >
            重新读取系统设置
          </button>
        </div>
      )}
      {resource.status === "ready" && (
        <SystemEditor api={api} initial={resource.data} />
      )}
    </section>
  );
}
