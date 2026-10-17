import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Archive, Search } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type {
  Experience,
  ExperienceRecord,
  ExperienceRecordPage,
  ExperienceRecordText,
} from "../../api/types";
import { ErrorNotice } from "../../shared/ui";
import {
  continuationApi,
  experienceKindLabel,
} from "./api";

const pageBytes = 8192;

function dateRange(item: Experience) {
  const date = (value?: string | null) => {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(parsed);
  };
  const start = date(item.occurred_start);
  const end = date(item.occurred_end);
  return start && end && start !== end ? `${start} – ${end}` : start || end || "事件时间未记录";
}

function coverageText(value: string) {
  return (
    {
      retained: "原文完整保留",
      bounded: "有界保留",
      capture_gap: "有采集缺口",
      redacted: "部分脱敏",
      revoked: "已撤回",
      missing: "缺少记录",
    }[value] ?? value
  );
}

/** Read-only by construction: this module never touches chat/model endpoints.
 * Cursors are opaque values returned by the server; pages append in order. */
export function ExperienceViewer({
  api,
  agentId,
  initialId,
  embedded = false,
  initialKind,
}: {
  api: LingApi;
  agentId: string;
  initialId?: string;
  embedded?: boolean;
  initialKind?: string;
}) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [items, setItems] = useState<Experience[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [kind, setKind] = useState(initialKind ?? "");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Experience | null>(null);
  const active = useRef<AbortController | null>(null);
  const request = useRef(0);
  const initialResolved = useRef(false);
  const seen = useRef(new Set<string>());

  useEffect(() => {
    setKind(initialKind ?? "");
  }, [initialKind]);

  const read = useCallback(
    async (cursor: string | null = null, append = false) => {
      if (cursor && seen.current.has(cursor)) { setError("服务返回重复目录游标，请刷新目录。"); return; }
      if (!append) seen.current.clear();
      active.current?.abort();
      const controller = new AbortController();
      active.current = controller;
      setStatus("loading");
      setError("");
      const sequence = ++request.current;
      try {
        const value = await client.experiences(agentId, kind || undefined, cursor, 20, controller.signal);
        if (controller.signal.aborted || sequence !== request.current) return;
        if (cursor) seen.current.add(cursor);
        setItems((current) => append ? [...current, ...value.items.filter((row) => !current.some((old) => old.id === row.id))] : value.items);
        setNext(value.next_cursor ?? null);
        setStatus("ready");
      } catch (cause) {
        if (!controller.signal.aborted && sequence === request.current) {
          setStatus("error");
          setError(errorMessage(cause));
        }
      }
    },
    [agentId, client, kind],
  );

  useEffect(() => {
    void read();
    return () => active.current?.abort();
  }, [read]);

  useEffect(() => {
    if (!initialId || initialResolved.current || status !== "ready") return;
    const found = items.find((item) => item.id === initialId || item.branch_id === initialId);
    if (found) { initialResolved.current = true; setOpen(found); }
    else if (next) void read(next, true);
    else { initialResolved.current = true; setError("归档尚未出现在经历目录；可刷新后重试。"); }
  }, [initialId, items, next, read, status]);

  if (open)
    return (
      <ExperienceDetails
        key={open.id}
        api={api}
        item={open}
        onBack={() => setOpen(null)}
        onChanged={(value) => {
          setOpen(value);
          setItems((current) => current.map((item) => (item.id === value.id ? value : item)));
        }}
        embedded={embedded}
      />
    );

  const visible = query.trim()
    ? items.filter((item) =>
        `${item.title} ${item.summary}`.toLowerCase().includes(query.trim().toLowerCase()),
      )
    : items;

  return (
    <section className={`continuity-experiences${embedded ? " embedded" : ""}`} aria-label="经历">
      {!embedded && (
        <header className="continuity-page-head">
          <p className="eyebrow">可回查的连续经历</p>
          <h1 tabIndex={-1}>经历</h1>
          <p className="muted">
            工作分支和主干事件各有独立摘要与发生时间。打开这里只读取本地记录，不调用模型。
          </p>
        </header>
      )}
      <div className="continuity-experience-tools">
        <label className="continuity-search">
          <Search size={14} aria-hidden="true" />
          <span className="sr-only">筛选经历</span>
          <input
            value={query}
            placeholder="按标题或摘要筛选已载入条目"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label>
          <span className="sr-only">经历类型</span>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="">全部类型</option>
            <option value="work_branch">工作分支</option>
            <option value="main_event">主干事件</option>
            <option value="legacy_work_event">旧工作记录</option>
          </select>
        </label>
        <button className="button secondary small-button" disabled={status === "loading"} onClick={() => void read()}>
          刷新目录
        </button>
      </div>
      {error && <ErrorNotice message={error} retry={() => void read()} />}
      {status === "loading" && !items.length && <p className="continuity-loading" role="status">正在读取经历目录…</p>}
      {status === "ready" && !visible.length && (
        <div className="continuity-empty-state">
          <Archive size={26} aria-hidden="true" />
          <h2>{items.length ? "没有匹配已载入条目" : "还没有可回忆的经历"}</h2>
          <p>完成的工作分支或达到条件的旧主干事件会出现在这里。</p>
        </div>
      )}
      <ul className="continuity-experience-list">
        {visible.map((item) => (
          <li key={item.id}>
            <button className="continuity-experience-card" onClick={() => setOpen(item)}>
              <span className="continuity-experience-head">
                <span className="pill">{experienceKindLabel(item.kind)}</span>
                <span className="metadata">{dateRange(item)}</span>
                {!item.valid && <span className="pill warm">已失效</span>}
              </span>
              <strong>{item.title}</strong>
              <span className="continuity-experience-summary">{item.valid ? item.summary : "摘要已撤回或来源失效"}</span>
              <span className="metadata">
                {item.source_count} 条来源 · {coverageText(item.coverage)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {next && (
        <button
          className="button secondary continuity-load-more"
          disabled={status === "loading"}
          onClick={() => void read(next, true)}
        >
          {status === "loading" ? "正在读取…" : "再读 20 条"}
        </button>
      )}
    </section>
  );
}

function ExperienceDetails({
  api,
  item,
  onBack,
  onChanged,
  embedded,
}: {
  api: LingApi;
  item: Experience;
  onBack: () => void;
  onChanged: (value: Experience) => void;
  embedded: boolean;
}) {
  const client = useMemo(() => continuationApi(api), [api]);
  const [page, setPage] = useState<ExperienceRecordPage | null>(null);
  const [records, setRecords] = useState<ExperienceRecord[]>([]);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const [opened, setOpened] = useState<
    (ExperienceRecordText & { sourceId: string; location: string }) | null
  >(null);
  const [textBusy, setTextBusy] = useState(false);
  const textController = useRef<AbortController | null>(null);
  const recordCursors = useRef(new Set<string>());
  const [revoking, setRevoking] = useState(false);
  const [notice, setNotice] = useState("");
  const controller = useRef<AbortController | null>(null);

  const load = useCallback(
    async (cursor: string | null = null) => {
      if (cursor && recordCursors.current.has(cursor)) { setError("服务返回重复来源游标，请重新打开经历。"); return; }
      controller.current?.abort();
      const next = new AbortController();
      controller.current = next;
      setReading(true);
      setError("");
      try {
        const value = await client.experienceRecords(
          item.id,
          item.conversation_id ?? null,
          cursor,
          20,
          next.signal,
        );
        if (next.signal.aborted) return;
        if (cursor) recordCursors.current.add(cursor);
        if (value.revision !== item.revision) throw new Error("经历版本已变化，请重新打开经历。");
        setPage(value);
        setRecords((current) => cursor ? [...current, ...value.items] : value.items);
      } catch (cause) {
        if (!next.signal.aborted) setError(errorMessage(cause));
      } finally {
        if (!next.signal.aborted) setReading(false);
      }
    },
    [client, item.conversation_id, item.id, item.revision],
  );

  useEffect(() => {
    void load();
    return () => { controller.current?.abort(); textController.current?.abort(); };
  }, [load]);

  const openRecord = async (record: ExperienceRecord, cursor: string | null = null) => {
    textController.current?.abort();
    const currentRequest = new AbortController();
    textController.current = currentRequest;
    setTextBusy(true);
    setError("");
    if (!cursor) setOpened(null);
    try {
      const value = await client.experienceRecord(
        item.id,
        record.source_id,
        item.conversation_id ?? null,
        cursor,
        pageBytes,
        currentRequest.signal,
      );
      if (currentRequest.signal.aborted) return;
      if (value.revision !== item.revision) throw new Error("经历版本已变化，请重新打开经历。");
      setOpened((current) =>
        current && current.sourceId === record.source_id && cursor
          ? current.sha256 !== value.sha256 || value.next_cursor === cursor
            ? null
            : { ...value, sourceId: record.source_id, location: record.location, text: current.text + value.text }
          : { ...value, sourceId: record.source_id, location: record.location },
      );
    } catch (cause) {
      if (!currentRequest.signal.aborted) setError(errorMessage(cause));
    } finally { if (!currentRequest.signal.aborted) setTextBusy(false); }
  };

  const revoke = async () => {
    if (revoking) return;
    setRevoking(true);
    setError("");
    try {
      const value = await client.revoke(item.id, item.revision);
      textController.current?.abort();
      setOpened(null);
      onChanged(value);
      setNotice("摘要已撤回，原文和审计记录仍保留；不会自动再次调用模型整理。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setRevoking(false);
    }
  };

  return (
    <article className={`continuity-experience-detail${embedded ? " embedded" : ""}`} aria-label="经历详情">
      <button className="text-button" onClick={onBack}>
        <ArrowLeft size={14} />返回经历目录
      </button>
      <header>
        <span className="pill">{experienceKindLabel(item.kind)}</span>
        <h1 tabIndex={-1}>{item.title}</h1>
        <p className="metadata">{dateRange(item)} · {coverageText(item.coverage)}</p>
      </header>
      {!item.valid && (
        <p className="notice warm">
          这条经历已失效：{item.reason || "来源或授权发生变化。"}
        </p>
      )}
      <p className="preserve-text continuity-experience-full-summary">{item.valid ? item.summary : "摘要已撤回或来源失效。原文按当前授权保留。"}</p>
      <div className="continuity-detail-actions">
        <button
          className="button secondary small-button danger-text"
          disabled={revoking || !item.valid}
          onClick={() => void revoke()}
        >
          {revoking ? "正在撤回…" : "撤回这条摘要"}
        </button>
        <span className="metadata">版本 {item.revision} · 查看原文不会调用模型</span>
      </div>
      {notice && <p className="notice success" role="status">{notice}</p>}
      {error && <ErrorNotice message={error} />}
      <section className="continuity-records" aria-label="来源目录">
        <h2>来源与原文</h2>
        {reading && !records.length && <p className="continuity-loading">正在读取来源目录…</p>}
        {!reading && !records.length && <p className="muted">服务端没有返回可读取来源。</p>}
        <ul>
          {records.map((record) => (
            <li key={record.id || record.source_id}>
              <button className="continuity-record-row" onClick={() => void openRecord(record)}>
                <span className="continuity-record-kind">{record.kind || "记录"}</span>
                <span className="continuity-record-location">{record.location || record.source_id}</span>
                <span className="metadata">{coverageText(record.coverage)}</span>
              </button>
            </li>
          ))}
        </ul>
        {page?.next_cursor && (
          <button className="text-button" disabled={reading} onClick={() => void load(page.next_cursor ?? null)}>
            {reading ? "读取中…" : "读取更多来源"}
          </button>
        )}
      </section>
      {opened && (
        <section className="continuity-record-text" aria-label="来源正文">
          <div className="continuity-record-text-head">
            <strong>{opened.location}</strong>
            <span className="metadata">SHA-256 {opened.sha256.slice(0, 12)} · {coverageText(opened.coverage)}</span>
          </div>
          <pre>{opened.text}</pre>
          <div className="button-row">
            {opened.next_cursor && (
              <button
                className="text-button"
                disabled={textBusy}
                onClick={() => {
                  const record = records.find((value) => value.source_id === opened.sourceId);
                  if (record) void openRecord(record, opened.next_cursor ?? null);
                }}
              >
                读取下一页
              </button>
            )}
            <button className="text-button" onClick={() => setOpened(null)}>收起正文</button>
          </div>
          <p className="metadata">每页最多 {opened.page_bytes.toLocaleString()} 字节，服务端游标保持来源边界。</p>
        </section>
      )}
    </article>
  );
}
