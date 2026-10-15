import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type {
  SandboxLevel,
  SandboxPreset,
  SandboxStatus,
} from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice, Loading } from "../../shared/ui";
import { useResource } from "../../shared/useResource";
import "./management.css";

const levelNames: Record<SandboxLevel, string> = {
  local: "本地受限执行",
  container: "容器隔离",
};
const presetNames: Record<SandboxPreset, string> = {
  conservative: "保守档",
  coding: "编程档",
};

function formatBytes(bytes: number) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KiB`;
  return `${bytes} B`;
}

function SandboxEditor({
  api,
  initial,
  reload,
}: {
  api: LingApi;
  initial: SandboxStatus;
  reload: () => Promise<void>;
}) {
  const [status, setStatus] = useState(initial);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  // The backend is the single source of truth: a tier is offered only when it
  // reports it as available, so an absent engine keeps the container tier off.
  const containerAvailable = status.available_levels.includes("container");

  async function apply(level: SandboxLevel) {
    if (busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.setSandboxLevel(level, controller.signal);
      if (controller.signal.aborted) return;
      setStatus(result);
      setNotice(
        "执行沙箱已切换到「" +
          levelNames[result.level] +
          "」，设置已保存，重启后保持。",
      );
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function applyPreset(preset: SandboxPreset) {
    if (busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.setSandboxPreset(
        status.level,
        preset,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (!result.preset) {
        setError("当前服务版本未确认额度档位设置，请更新后端再切换。");
        return;
      }
      setStatus(result);
      setNotice(
        `执行额度已切换到「${presetNames[result.preset]}」，设置已保存。`,
      );
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <>
      <p className="metadata">
        当前档位：{levelNames[status.level]}
        {status.engine.name ? " · 引擎 " + status.engine.name : ""}
        {" · 容器镜像 " + status.image}
      </p>
      <div className="sandbox-preset">
        <label htmlFor="sandbox-preset">命令资源档位</label>
        <select
          id="sandbox-preset"
          value={status.preset ?? "conservative"}
          disabled={busy}
          onChange={(event) =>
            void applyPreset(event.target.value as SandboxPreset)
          }
        >
          <option value="conservative">保守档</option>
          <option value="coding">编程档</option>
        </select>
        <p className="muted small">
          {status.preset
            ? `当前使用${presetNames[status.preset]}。档位只调整命令资源额度，不更改隔离档位。`
            : "当前服务未返回额度档位，按保守档显示；可用额度信息如下。"}
        </p>
        {status.limits ? (
          <dl className="sandbox-limits">
            <div>
              <dt>单条命令时限</dt>
              <dd>{status.limits.timeout_seconds} 秒</dd>
            </div>
            <div>
              <dt>内存上限</dt>
              <dd>{formatBytes(status.limits.memory_bytes)}</dd>
            </div>
            <div>
              <dt>单次返回输出</dt>
              <dd>{formatBytes(status.limits.max_output_bytes)}</dd>
            </div>
            <div>
              <dt>会话总时限</dt>
              <dd>{status.limits.session_timeout_seconds} 秒</dd>
            </div>
            <div>
              <dt>并行命令会话</dt>
              <dd>最多 {status.limits.max_sessions} 个</dd>
            </div>
          </dl>
        ) : (
          <p className="field-help">当前服务未提供命令资源额度详情。</p>
        )}
      </div>
      <div className="sandbox-tiers">
        <div
          className={
            "sandbox-tier" + (status.level === "local" ? " selected" : "")
          }
        >
          <div className="section-heading">
            <h3>本地受限执行</h3>
            {status.level === "local" && (
              <span className="pill warm">当前档位</span>
            )}
          </div>
          <p className="muted small">
            默认档位。不需要安装任何外部引擎，直接在本机受限运行。
          </p>
          <button
            className="button secondary"
            type="button"
            disabled={busy || status.level === "local"}
            onClick={() => void apply("local")}
          >
            切换到本地受限执行
          </button>
        </div>
        <div
          className={
            "sandbox-tier" + (status.level === "container" ? " selected" : "")
          }
        >
          <div className="section-heading">
            <h3>容器隔离</h3>
            {status.level === "container" && (
              <span className="pill warm">当前档位</span>
            )}
          </div>
          <p className="muted small">
            更强隔离：文件系统与网络隔离，以非 root 用户运行。
          </p>
          {!containerAvailable && (
            <p className="field-help">
              需要先安装容器引擎（Docker 或
              Podman）并确保其正在运行；安装后点“刷新”即可选择。
            </p>
          )}
          <button
            className="button secondary"
            type="button"
            disabled={
              busy || !containerAvailable || status.level === "container"
            }
            onClick={() => setConfirming(true)}
          >
            切换到容器隔离
          </button>
        </div>
      </div>
      <div className="sandbox-boundaries">
        <div>
          <h4>
            <ShieldCheck size={17} strokeWidth={1.5} aria-hidden="true" />
            它能防住
          </h4>
          <ul>
            {status.protections.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
        <div>
          <h4>
            <ShieldAlert size={17} strokeWidth={1.5} aria-hidden="true" />
            它防不住
          </h4>
          <ul>
            {status.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </div>
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
      <div className="button-row">
        <button
          className="button secondary"
          type="button"
          disabled={busy}
          onClick={() => void reload()}
        >
          <RefreshCw size={15} aria-hidden="true" />
          刷新档位与引擎状态
        </button>
      </div>
      {confirming && (
        <ConfirmDialog
          title="切换到容器隔离"
          confirmLabel="确认切换到容器隔离"
          busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            void (async () => {
              await apply("container");
              setConfirming(false);
            })();
          }}
        >
          <p>
            容器档提供比默认档更严格的隔离：文件系统与网络隔离，并以非 root
            用户运行。
          </p>
          <p>
            切换后设置会保存，重启后保持。之后的任务将改用容器执行，需要本机容器引擎持续可用。
          </p>
        </ConfirmDialog>
      )}
    </>
  );
}

export function SandboxSettings({ api }: { api: LingApi }) {
  const load = useCallback((signal: AbortSignal) => api.sandbox(signal), [api]);
  const resource = useResource(load);
  return (
    <section
      className="panel section-panel sandbox-settings"
      aria-labelledby="sandbox-heading"
    >
      <div className="section-heading">
        <h2 id="sandbox-heading">执行沙箱</h2>
        {resource.status === "ready" && (
          <span className="pill">{levelNames[resource.data.level]}</span>
        )}
      </div>
      <p className="muted">
        编程任务在本机运行时采用的隔离档位。默认使用本地受限执行，无需外部引擎；也可以升级到容器隔离。
      </p>
      {resource.status === "loading" && (
        <Loading label="正在读取执行沙箱档位…" />
      )}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <SandboxEditor
          api={api}
          initial={resource.data}
          reload={resource.refresh}
        />
      )}
    </section>
  );
}
