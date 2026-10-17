import { useEffect, useMemo, useState } from "react";
import { errorMessage, type LingApi } from "../../api/client";
import type { ConversationMessage, ReflectionCandidate, ReflectionQualification, ReflectionRun } from "../../api/types";
import { ErrorNotice, formatDate } from "../../shared/ui";
import { continuationApi } from "./api";

/** The owner records their assessment. Merely saving settings never qualifies
 * a profile, and this form makes no claim about tests executed by Codex. */
export function ReflectionQualificationCard({ api, agentId, profileId }: {
  api: LingApi; agentId: string; profileId: string;
}) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [value, setValue] = useState<ReflectionQualification | null>(null);
  const [saved, setSaved] = useState<ReflectionQualification | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setValue(null); setError("");
    void client.qualification(agentId, profileId, controller.signal).then((result) => {
      if (!controller.signal.aborted) {
        const value = { ...result };
        setValue(value); setSaved(value);
      }
    }).catch((cause) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [client, agentId, profileId, retry]);
  const qualifies = value && Number.isInteger(value.sample_count) && value.sample_count >= 30 &&
    Number.isInteger(value.acceptable_count) && value.acceptable_count <= value.sample_count &&
    value.acceptable_count / value.sample_count >= .9 && value.critical_errors === 0 && !!value.evidence.trim();
  const save = async (qualified: boolean) => {
    if (!value || busy || qualified && !qualifies) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await client.saveQualification(agentId, { ...value, qualified });
      setSaved(result); setValue(result);
      setNotice(qualified ? "已保存你的验证确认。连接配置变化后，需要重新确认。" : "已关闭这条连接的自动发布资格；新的摘要仍需审阅。");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <section className="continuity-quality" aria-label="整理质量确认">
    <h3>这条连接可以自动发布吗？</h3>
    <p className="muted small">尚未验证时，每次整理都会留下待审阅的摘要。完成至少 30 个样本检查，九成无需重大修改且没有关键错误后，你可以在此记录证据并确认。</p>
    <p className="pill">{saved?.qualified && saved.valid !== false ? "你已确认可自动发布" : "尚未验证，先人工审阅"}</p>
    {!value && !error && <p role="status">正在读取验证记录…</p>}
    {error && <ErrorNotice message={error} retry={() => setRetry((number) => number + 1)} />}
    {notice && <p className="notice" role="status">{notice}</p>}
    {value && <fieldset disabled={busy} className="continuity-quality-form">
      <legend className="sr-only">记录你自己的验证结果</legend>
      <label>已检查样本数<input type="number" min={30} value={value.sample_count} onChange={(event) => setValue({ ...value, sample_count: Number(event.target.value) })} /></label>
      <label>无需重大修改的样本数<input type="number" min={0} value={value.acceptable_count} onChange={(event) => setValue({ ...value, acceptable_count: Number(event.target.value) })} /></label>
      <label>关键错误数<input type="number" min={0} value={value.critical_errors} onChange={(event) => setValue({ ...value, critical_errors: Number(event.target.value) })} /></label>
      <label>验证记录或证据<textarea value={value.evidence} onChange={(event) => setValue({ ...value, evidence: event.target.value })} placeholder="写下样本、判定标准和检查结果的保存位置。" /></label>
      <p className="metadata">这里记录的是你的确认。本轮离线结构测试不会代替真实连接的摘要质量验证。</p>
      <div className="button-row">
        <button className="button secondary" disabled={!qualifies} onClick={() => void save(true)}>确认这条连接可自动发布</button>
        {saved?.qualified && <button className="text-button danger-text" onClick={() => void save(false)}>恢复为每次人工审阅</button>}
      </div>
    </fieldset>}
  </section>;
}

export function ReflectionCandidateReview({ api, agentId, run, onPublished }: {
  api: LingApi; agentId: string; run: ReflectionRun; onPublished: (value: ReflectionRun) => void;
}) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [events, setEvents] = useState<ReflectionCandidate[]>(() => structuredClone(run.candidate_events ?? []));
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  useEffect(() => {
    if (!showSources) return;
    const controller = new AbortController();
    setLoading(true);
    void client.main(agentId, controller.signal).then((main) => {
      if (!main.conversation) throw new Error("主干尚未选定。");
      return api.conversationMessages(main.conversation.id, controller.signal);
    }).then((value) => { if (!controller.signal.aborted) setMessages(value); })
      .catch((cause) => { if (!controller.signal.aborted) setError(errorMessage(cause)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [showSources, client, api, agentId]);
  const publish = async () => {
    if (busy || !reviewed || run.revision === undefined) return;
    setBusy(true); setError("");
    try { onPublished(await client.publishReflection(run.id, run.revision, events)); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <section className="continuity-review" aria-label="审阅整理摘要">
    <h3>确认这些经历</h3>
    <p className="muted small">这些摘要尚未进入主干。可以先查看原文，再改正标题和摘要；发布会同时保存这一整批经历。</p>
    <button className="text-button" onClick={() => setShowSources(!showSources)}>{showSources ? "收起对应原文" : "查看对应原文"}</button>
    {loading && <p role="status">正在读取本地原文…</p>}
    {events.map((event, index) => {
      const sourceIds = new Set(event.source_ids.map((id) => id.replace(/^turn:/, "")));
      let include = false;
      const sources = messages.filter((message) => {
        if (message.role === "user") include = sourceIds.has(message.id);
        return include;
      });
      return <fieldset key={index} disabled={busy} className="continuity-candidate">
        <legend>经历 {index + 1}</legend>
        {(event.occurred_start || event.occurred_end) && <p className="metadata">{formatDate(event.occurred_start ?? undefined)} — {formatDate(event.occurred_end ?? undefined)}</p>}
        <label>标题<input value={event.title} onChange={(input) => { setReviewed(false); setEvents((all) => all.map((item, n) => n === index ? { ...item, title: input.target.value } : item)); }} /></label>
        <label>摘要<textarea value={event.summary} onChange={(input) => { setReviewed(false); setEvents((all) => all.map((item, n) => n === index ? { ...item, summary: input.target.value } : item)); }} /></label>
        <details><summary>{event.source_ids.length} 轮来源</summary><p className="metadata break-anywhere">{event.source_ids.join("、")}</p></details>
        {showSources && <div className="continuity-candidate-sources">
          {sources.map((message) => <p key={message.id} className="preserve-text"><strong>{message.role === "user" ? "你" : "灵"}：</strong>{message.text}</p>)}
          {!sources.length && !loading && <p className="metadata">当前主干路径未返回这些原文；请确认来源仍可访问。</p>}
        </div>}
      </fieldset>;
    })}
    {error && <ErrorNotice message={error} />}
    <label className="continuity-switch"><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} /><span>我已审阅这一批摘要，确认内容和来源对应</span></label>
    <button className="button primary" disabled={busy || !reviewed || !events.length || events.some((event) => !event.title.trim() || !event.summary.trim()) || run.revision === undefined} onClick={() => void publish()}>{busy ? "正在发布…" : "发布这批经历"}</button>
  </section>;
}
