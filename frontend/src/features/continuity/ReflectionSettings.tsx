import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarClock, CircleStop, Play, ScanSearch } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type {
  Agent,
  ReflectionPolicy,
  ReflectionPreview,
  ReflectionRun,
  SavedModel,
} from "../../api/types";
import { ErrorNotice } from "../../shared/ui";
import {
  continuationApi,
  continuityRequestId,
  reflectionStatusLabel,
} from "./api";
import "./continuitySettings.css";
import { ReflectionCandidateReview, ReflectionQualificationCard } from "./ReflectionReview";

function reflectionReason(reason: string) {
  return ({ quality_unverified: "这条连接尚未通过你的质量确认，请先审阅摘要。", below_gate: "旧历史未达整理门槛，本次未调用模型。", disabled: "自动整理已关闭。", no_profile: "尚未选择整理连接。", daily_budget: "已达今日调用上限。", call_budget: "已达本次调用上限。", sources_changed: "来源已改变，需要重新整理。" } as Record<string, string>)[reason] ?? "整理未完成：" + reason;
}

const defaults: ReflectionPolicy = {
  enabled: false,
  revision: 0,
  local_time: "04:00",
  timezone: "Asia/Tokyo",
  min_age_days: 3,
  threshold_bytes: 32768,
  profile_id: null,
  max_calls: 8,
  max_source_bytes: 131072,
  max_events: 6,
  daily_call_limit: 24,
};

function validPolicy(value: ReflectionPolicy) {
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value.local_time)) return "执行时间必须是 HH:MM。";
  try { new Intl.DateTimeFormat("zh-CN", { timeZone: value.timezone }).format(); }
  catch { return "请填写有效 IANA 时区，例如 Asia/Tokyo。"; }
  if (!Number.isInteger(value.min_age_days) || value.min_age_days < 0 || value.min_age_days > 3650)
    return "最短年龄应在 0–3650 天之间。";
  if (!Number.isInteger(value.threshold_bytes) || value.threshold_bytes < 1 || value.threshold_bytes > 16777216)
    return "触发阈值必须是正整数字节。";
  if (!Number.isInteger(value.max_source_bytes) || value.max_source_bytes < 1 || value.max_source_bytes > 67108864)
    return "单作业来源上限须在 1–67108864 字节之间。";
  if (!Number.isInteger(value.max_events) || value.max_events < 1 || value.max_events > 64)
    return "每次事件上限应在 1–64 之间。";
  if (!Number.isInteger(value.max_calls) || value.max_calls < 1 || value.max_calls > 64)
    return "单作业调用上限应在 1–64 之间。";
  if (!Number.isInteger(value.daily_call_limit) || value.daily_call_limit < value.max_calls || value.daily_call_limit > 512)
    return "每日调用上限须不小于单次调用上限，且不超过 512。";
  if (value.enabled && !value.profile_id) return "启用自动整理前必须选择一个模型连接。";
  return "";
}

function PolicyFields({
  policy,
  models,
  onChange,
}: {
  policy: ReflectionPolicy;
  models: SavedModel[];
  onChange: (value: ReflectionPolicy) => void;
}) {
  const set = <K extends keyof ReflectionPolicy>(key: K, value: ReflectionPolicy[K]) =>
    onChange({ ...policy, [key]: value });
  const number = (key: keyof ReflectionPolicy, value: string) =>
    set(key, (value === "" ? 0 : Number(value)) as never);
  return (
    <div className="continuity-policy-fields">
      <label className="continuity-switch">
        <input
          type="checkbox"
          checked={policy.enabled}
          onChange={(event) => set("enabled", event.target.checked)}
        />
        <span>
          <strong>定时整理主干旧历史</strong>
          <small className="metadata">默认关闭；达到本地闸门后才调用整理模型。</small>
        </span>
      </label>
      <label>
        执行时间
        <input type="time" value={policy.local_time} onChange={(event) => set("local_time", event.target.value)} />
      </label>
      <label>
        时区
        <input
          value={policy.timezone}
          placeholder="Asia/Tokyo"
          autoComplete="off"
          onChange={(event) => set("timezone", event.target.value.trim())}
        />
        <span className="field-help">日历按此时区计算；预算日账仍按 UTC 日桶。</span>
      </label>
      <label>
        最短历史年龄（天）
        <input type="number" min={0} max={3650} value={policy.min_age_days} onChange={(event) => number("min_age_days", event.target.value)} />
      </label>
      <label>
        触发阈值（字节）
        <input type="number" min={1} value={policy.threshold_bytes} onChange={(event) => number("threshold_bytes", event.target.value)} />
        <span className="field-help">本地 UTF-8 字节门槛，不是 Provider Token 计数。</span>
      </label>
      <label>
        整理模型
        <select value={policy.profile_id ?? ""} onChange={(event) => set("profile_id", event.target.value || null)}>
          <option value="">不选择（不调用模型）</option>
          {models.map((model) => (
            <option key={model.id} value={model.id}>{model.name}</option>
          ))}
        </select>
      </label>
      <label>
        每次作业最多调用
        <input type="number" min={1} max={64} value={policy.max_calls} onChange={(event) => number("max_calls", event.target.value)} />
      </label>
      <label>
        单作业最多来源（字节）
        <input type="number" min={1} value={policy.max_source_bytes} onChange={(event) => number("max_source_bytes", event.target.value)} />
      </label>
      <label>
        每次最多发布事件
        <input type="number" min={1} max={64} value={policy.max_events} onChange={(event) => number("max_events", event.target.value)} />
      </label>
      <label>
        UTC 每日调用上限
        <input type="number" min={1} value={policy.daily_call_limit} onChange={(event) => number("daily_call_limit", event.target.value)} />
      </label>
    </div>
  );
}

function Preview({ value }: { value: ReflectionPreview }) {
  return (
    <aside className="continuity-preview" aria-label="本地反思预览">
      <div className="continuity-preview-title">
        <ScanSearch size={16} aria-hidden="true" />
        <strong>本地候选预览</strong>
        <span className={`pill ${value.gate_passed ? "" : "warm"}`}>
          {value.gate_passed ? "已达触发门槛" : "未达门槛"}
        </span>
      </div>
      <dl className="continuity-metrics">
        <div><dt>本次候选回合</dt><dd>{value.source_units}</dd></div>
        <div><dt>候选规模</dt><dd>{value.candidate_bytes.toLocaleString()} B</dd></div>
        <div><dt>阈值</dt><dd>{value.threshold_bytes.toLocaleString()} B</dd></div>
        <div><dt>模型调用</dt><dd>{value.model_calls}</dd></div>
      </dl>
      <p className="metadata">
        {value.reason || "预览只做本地年龄、来源与规模检查。"}
        {value.deferred?.length ? ` ${value.deferred.length} 个过大来源保持原文并延期。` : ""}
      </p>
    </aside>
  );
}

export function ReflectionSettings({ api, agent }: { api: LingApi; agent: Agent }) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [policy, setPolicy] = useState<ReflectionPolicy>(defaults);
  const [original, setOriginal] = useState<ReflectionPolicy | null>(null);
  const [models, setModels] = useState<SavedModel[]>([]);
  const [runs, setRuns] = useState<ReflectionRun[]>([]);
  const [preview, setPreview] = useState<ReflectionPreview | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewRun, setReviewRun] = useState<string | null>(null);
  const [qualityOpen, setQualityOpen] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const jobsController = useRef<AbortController | null>(null);

  const refreshJobs = async () => {
    jobsController.current?.abort();
    const next = new AbortController();
    jobsController.current = next;
    try {
      const jobs = await client.reflectionRuns(agent.id, next.signal);
      if (!next.signal.aborted) setRuns(jobs);
    } catch (cause) { if (!next.signal.aborted) setError(errorMessage(cause)); }
  };
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refreshJobs();
    }, 5000);
    return () => { window.clearInterval(timer); jobsController.current?.abort(); };
  }, [client, agent.id]);

  const load = async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setStatus("loading");
    setError("");
    try {
      const [saved, available, jobs] = await Promise.all([
        client.reflectionSettings(agent.id, next.signal),
        api.models(next.signal),
        client.reflectionRuns(agent.id, next.signal),
      ]);
      if (next.signal.aborted) return;
      setPolicy(saved);
      setOriginal(saved);
      setModels(available);
      setRuns(jobs);
      setStatus("ready");
    } catch (cause) {
      if (!next.signal.aborted) {
        setStatus("error");
        setError(errorMessage(cause));
      }
    }
  };

  useEffect(() => {
    void load();
    return () => controller.current?.abort();
  }, [agent.id, client]);

  const dirty = JSON.stringify(policy) !== JSON.stringify(original);
  const save = async () => {
    const problem = validPolicy(policy);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await client.saveReflectionSettings(agent.id, policy);
      setPolicy(saved);
      setOriginal(saved);
      setNotice("反思设置已保存。时区、年龄或阈值变化会从下一次计划检查起生效。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const scan = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setPreview(await client.reflectionPreview(agent.id));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    if (!policy.enabled || !policy.profile_id) {
      setError("需先启用并选择整理模型，才能运行。");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await client.runReflection(agent.id, continuityRequestId());
      setRuns((current) => [value, ...current.filter((item) => item.id !== value.id)]);
      setNotice(
        value.status === "skipped" && value.error_code === "below_gate"
          ? "本地闸门未达到，本次没有调用模型，也没有推进覆盖。"
          : value.status === "skipped" ? "本轮已跳过，请查看下方原因。" : "已提交一次整理作业；运行不会阻塞日常对话。",
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (run: ReflectionRun) => {
    setError("");
    try {
      const value = await client.cancelReflection(agent.id, run.id);
      setRuns((current) => current.map((item) => item.id === value.id ? value : item));
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  return (
    <section className="panel section-panel continuity-settings" aria-label="反思设置">
      <div className="section-heading">
        <h2><CalendarClock size={17} aria-hidden="true" /> 主干反思</h2>
        <span className="pill">{policy.enabled ? "已启用" : "默认关闭"}</span>
      </div>
      <p className="muted">
        只在主干旧历史满足年龄与规模条件时整理。低于门槛时零模型调用；原文始终保留，可从经历页逐条回查。
      </p>
      {status === "loading" && <p className="continuity-loading" role="status">正在读取反思设置…</p>}
      {status === "error" && <ErrorNotice message={error} retry={() => void load()} />}
      {status === "ready" && (
        <>
          <PolicyFields policy={policy} models={models} onChange={setPolicy} />
          {error && <ErrorNotice message={error} />}
          {notice && <p className="notice success" role="status">{notice}</p>}
          <div className="button-row">
            <button className="button primary" disabled={busy || JSON.stringify(policy) === JSON.stringify(original)} onClick={() => void save()}>
              {busy ? "处理中…" : "保存反思设置"}
            </button>
            <button className="button secondary" disabled={busy || dirty} onClick={() => void scan()}>
              <ScanSearch size={15} />本地预览
            </button>
            <button className="button secondary" disabled={busy || dirty || !original?.enabled || !original.profile_id} onClick={() => void runNow()}>
              <Play size={15} />立即运行一次
            </button>
          </div>
          {preview && <Preview value={preview} />}
          <div className="continuity-runs">
            <div className="section-heading"><h3>最近运行</h3><button className="text-button" onClick={() => void refreshJobs()}>刷新整理状态</button></div>
            {!runs.length && <p className="muted small">还没有整理作业。</p>}
            <ul>
              {runs.map((run) => (
                <li key={run.id}>
                  <div>
                    <strong>{reflectionStatusLabel(run.status)}</strong>
                    <span className="metadata">
                      {run.candidate_events?.length ?? run.event_count ?? "—"} 个事件 · 调用 {run.calls ?? run.calls_used ?? "未知"}/{run.max_calls ?? run.calls_limit ?? policy.max_calls}
                    </span>
                  </div>
                  {run.error_code && <p className="metadata">{reflectionReason(run.error_code)}</p>}
                  {run.status === "candidate" && <p className="metadata">摘要等待你的审阅，尚未进入主干。</p>}
                  {run.status === "candidate" && !!run.candidate_events?.length && <button className="text-button" onClick={() => setReviewRun(reviewRun === run.id ? null : run.id)}>{reviewRun === run.id ? "收起审阅" : "审阅并发布"}</button>}
                  {["queued", "running", "retry", "candidate"].includes(run.status) && !run.cancel_requested && (
                    <button className="text-button danger-text" onClick={() => void cancel(run)}>
                      <CircleStop size={13} />取消作业
                    </button>
                  )}
                  {reviewRun === run.id && run.status === "candidate" && <ReflectionCandidateReview key={run.id + ":" + run.revision} api={api} agentId={agent.id} run={run} onPublished={(value) => {
                    setRuns((all) => all.map((item) => item.id === value.id ? value : item));
                    setReviewRun(null); setNotice("这批经历已发布，可从经历页回查原文。");
                  }} />}
                </li>
              ))}
            </ul>
          </div>
          {original?.profile_id && <section>
            <button className="text-button" onClick={() => setQualityOpen(!qualityOpen)}>{qualityOpen ? "收起质量确认" : "整理质量与自动发布"}</button>
            {qualityOpen && <ReflectionQualificationCard key={original.profile_id} api={api} agentId={agent.id} profileId={original.profile_id} />}
          </section>}
          <p className="metadata">日历按 <strong>{policy.timezone}</strong> 计算；每日调用预算独立按 UTC 日桶累计。停机后合并补跑；摘要通过质量检查后才会发布，未通过的保留候选。</p>
        </>
      )}
    </section>
  );
}
