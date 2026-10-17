import { useEffect, useMemo, useRef, useState } from "react";
import { Gauge, Info } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type {
  Agent,
  CompactionMode,
  CompactionSettings as Settings,
  SavedModel,
} from "../../api/types";
import { ErrorNotice } from "../../shared/ui";
import { continuationApi } from "./api";
import "./continuitySettings.css";

const modeText: Record<CompactionMode, string> = {
  disabled: "不压缩",
  safe_epoch_restart: "安全代际续做",
  native_compaction: "原生压缩（未验不得开启）",
};

export function CompactionSettings({ api, agent }: { api: LingApi; agent: Agent }) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [value, setValue] = useState<Settings | null>(null);
  const [original, setOriginal] = useState<Settings | null>(null);
  const [models, setModels] = useState<SavedModel[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    const next = new AbortController();
    controller.current = next;
    setStatus("loading");
    Promise.all([client.compactionSettings(agent.id, next.signal), api.models(next.signal)])
      .then(([saved, profiles]) => {
        if (next.signal.aborted) return;
        setValue(saved);
        setOriginal(saved);
        setModels(profiles);
        setStatus("ready");
      })
      .catch((cause) => {
        if (next.signal.aborted) return;
        setError(errorMessage(cause));
        setStatus("error");
      });
    return () => next.abort();
  }, [agent.id, client]);

  if (status === "loading")
    return <section className="panel section-panel"><p className="continuity-loading" role="status">正在读取压缩策略…</p></section>;
  if (status === "error" || !value)
    return (
      <section className="panel section-panel continuity-settings" aria-label="上下文压缩设置">
        <div className="section-heading"><h2>工作分支压缩</h2></div>
        <ErrorNotice message={error || "压缩设置不可用。"} />
      </section>
    );

  const setMode = (id: string, mode: CompactionMode) =>
    setValue({ ...value, profile_modes: { ...value.profile_modes, [id]: mode } });
  const invalidRatio = !Number.isFinite(value.target_ratio) || !Number.isFinite(value.trigger_ratio) || value.target_ratio <= 0.05 || value.trigger_ratio <= 0.1 || value.trigger_ratio <= value.target_ratio || value.trigger_ratio >= 1;

  const save = async () => {
    if (invalidRatio) {
      setError("触发比例必须大于目标比例，且两者都在 0–1 之间。");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await client.saveCompactionSettings(agent.id, value);
      setValue(saved);
      setOriginal(saved);
      setNotice("压缩策略已保存。真实 profile 行为仍以单独实测结果为准。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel section-panel continuity-settings" aria-label="上下文压缩设置">
      <div className="section-heading">
        <h2><Gauge size={17} aria-hidden="true" /> 工作分支压缩</h2>
        <span className="pill">{value.enabled ? "已启用" : "关闭"}</span>
      </div>
      <p className="muted">
        压缩只发生在工作分支的安全边界，主干历史不会随之改变。默认不替未验证 profile 打开供应商原生压缩。
      </p>
      <label className="continuity-switch">
        <input type="checkbox" checked={value.enabled} onChange={(event) => setValue({ ...value, enabled: event.target.checked })} />
        <span><strong>允许工作分支进行中压缩</strong><small className="metadata">暂停、待审批、unknown 或不安全工具交换不会强行压缩。</small></span>
      </label>
      <div className="continuity-ratio-grid">
        <label>
          触发比例
          <input type="number" min={0.11} max={0.99} step={0.01} value={value.trigger_ratio} onChange={(event) => setValue({ ...value, trigger_ratio: Number(event.target.value) })} />
        </label>
        <label>
          目标比例
          <input type="number" min={0.06} max={0.99} step={0.01} value={value.target_ratio} onChange={(event) => setValue({ ...value, target_ratio: Number(event.target.value) })} />
        </label>
        <label>每次压缩摘要上限（字节）
          <input type="number" min={1024} max={65536} value={value.max_summary_bytes ?? 16384} onChange={(event) => setValue({ ...value, max_summary_bytes: Number(event.target.value) })} />
        </label>
        <label>每项工作最多压缩次数
          <input type="number" min={1} max={32} value={value.max_epochs ?? 16} onChange={(event) => setValue({ ...value, max_epochs: Number(event.target.value) })} />
        </label>
      </div>
      <p className="field-help">R0 基线使用保守比例；页面不允许目标高于触发。当前数值是本地策略，不是供应商 Token 实测结论。</p>
      <div className="continuity-profile-modes">
        <h3>按模型连接声明能力</h3>
        {!models.length && <p className="muted small">还没有模型连接。</p>}
        {models.map((model) => {
          const verification = value.profile_verification?.[model.id];
          const mode = value.profile_modes[model.id] ?? "disabled";
          return (
            <div className="continuity-profile-row" key={model.id}>
              <div>
                <strong>{model.name}</strong>
                <span className="metadata">
                  {verification?.live_verified
                    ? "真实通路已验证"
                    : verification?.offline_verified
                      ? "仅离线夹具验证；真实通路未验"
                      : value.validation === "offline_only" ? "仅离线验证；真实连接未验" : "尚未验证"}
                </span>
                {verification?.note && <span className="metadata">{verification.note}</span>}
              </div>
              <select value={mode} onChange={(event) => setMode(model.id, event.target.value as CompactionMode)}>
                {Object.entries(modeText).map(([key, label]) => (
                  <option
                    key={key}
                    value={key}
                    disabled={key === "native_compaction" && (!value.native_compaction_available || !verification?.live_verified)}
                  >
                    {label}
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </div>
      <p className="notice">
        <Info size={15} aria-hidden="true" />
        离线夹具通过不等于 provider 接受压缩请求。原生压缩只有真实通路验证后才可开启；无法安全压缩时会明确停止。
      </p>
      {error && <ErrorNotice message={error} />}
      {notice && <p className="notice success" role="status">{notice}</p>}
      <div className="button-row">
        <button
          className="button primary"
          disabled={busy || invalidRatio || JSON.stringify(value) === JSON.stringify(original)}
          onClick={() => void save()}
        >
          {busy ? "正在保存…" : "保存压缩策略"}
        </button>
      </div>
      {value.hard_checkpoint_bytes && (
        <p className="metadata">私有检查点硬上限由后端执行：{(value.hard_checkpoint_bytes / 1024 / 1024).toFixed(1)} MiB。</p>
      )}
    </section>
  );
}
