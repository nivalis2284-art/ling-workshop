import { useEffect, useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { ConversationMessage } from "../../api/types";
import { EventRecords } from "./EventRecords";
import { EventTaskLink } from "./EventTaskLink";
import {
  coverageLabel,
  hasGap,
  jobErrorLabel,
  jobRunning,
  lifecycleLabel,
  outcomeLabel,
  outcomeTone,
  reasonLabel,
  requestId,
  summaryJobLabel,
  summaryKindLabel,
  usageText,
  workEventSettingsApi,
} from "./api";
import type {
  WorkEvent,
  WorkEventSettings,
  WorkEventSummaryJob,
  WorkEventsClient,
} from "./api";
import { contextPreviewApi } from "./api";
import type { ContextPreview } from "./api";
import type { WorkEventList } from "./useWorkEvents";

function ContextEstimate({ preview }: { preview: ContextPreview | null }) {
  if (!preview) return null;
  if (!preview.available)
    return (
      <p className="metadata">选择模型连接后可预览当前分支的上下文预算。</p>
    );
  const projected = (preview.manifest?.work_events ?? []).filter(
    (item) => item.sent && item.strategy === "summary",
  );
  const removed = projected.reduce(
    (sum, item) => sum + (item.replaced_messages ?? 0),
    0,
  );
  return (
    <aside className="notice work-budget-estimate" aria-label="上下文预算估算">
      <p>
        当前已保存分支的保守估算（含输出预留）：
        {preview.estimated_total_with_output?.toLocaleString()} /{" "}
        {preview.context_limit?.toLocaleString()}。
      </p>
      <p className="metadata">
        {projected.length} 份概况替代 {removed} 条原消息；普通窗口裁剪{" "}
        {preview.manifest?.truncated_messages ?? 0} 条。
      </p>
      <p className="metadata">{preview.note}</p>
    </aside>
  );
}

/**
 * 对话内的工作事件面板（M2 阶段一）。
 *
 * 这里做两件彼此独立的事：
 * - 记录边界：标记「开始工作 / 结束工作」，结束后由服务端决定能否折叠。
 * - 回读原文：展开事件只读本地记录，永不发送聊天或模型请求。
 *
 * 面板 state 全部挂在 conversationId 上（ChatPage 以 key 重置），换对话不会
 * 串到另一段工作的列表、游标或草稿。
 */
/** 服务端返回的事件形状识别：摘要作业等其它载荷不能被当成事件写入列表。 */
function asWorkEvent(value: unknown): WorkEvent | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as WorkEvent;
  return typeof candidate.id === "string" &&
    typeof candidate.title === "string" &&
    typeof candidate.revision === "number" &&
    typeof candidate.folded === "boolean"
    ? candidate
    : undefined;
}

export function WorkEventsPanel({
  api,
  agentId,
  conversationId,
  messages,
  disabled,
  work,
  profileId,
}: {
  api: LingApi;
  agentId: string;
  conversationId: string;
  messages: ConversationMessage[];
  disabled: boolean;
  /** 与聊天记录共用的工作事件列表：同一份数据驱动面板和界面收起。 */
  work: WorkEventList;
  profileId?: string;
}) {
  const { client, events, setEvents, patch, loading, refresh } = work;
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<ContextPreview | null>(null);
  const [status, setStatus] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [title, setTitle] = useState("");
  const [startAt, setStartAt] = useState("");
  const [starting, setStarting] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [settings, setSettings] = useState<WorkEventSettings | null>(null);
  const [jobs, setJobs] = useState<WorkEventSummaryJob[]>([]);
  // 概况作业面板跟着请求走，而不是跟着原文展开走：查看记录和查看生成状态是
  // 两件独立的事。
  const [jobsId, setJobsId] = useState("");
  const [openId, setOpenId] = useState("");
  // 面板默认收起，展开后才读取列表与设置：这里的一条空行不应该在每次打开对话
  // 时都产生额外请求。
  const [loaded, setLoaded] = useState(false);
  const load = refresh;

  useEffect(() => {
    if (!expanded) return;
    // 摘要模型是可选能力：读不到设置就当作没有配置，绝不默认用主模型。
    let active = true;
    void workEventSettingsApi(api, agentId)
      .read()
      .then((value) => {
        if (active) setSettings(value);
      })
      .catch(() => {
        if (active) setSettings(null);
      });
    return () => {
      active = false;
    };
  }, [api, agentId, expanded]);

  useEffect(() => {
    if (!expanded || loaded) return;
    setLoaded(true);
    void load();
  }, [expanded, loaded, load]);

  // 估算只读已保存分支，只在面板展开或折叠状态变化后刷新；不参与任何模型调用。
  const refreshPreview = async () => {
    try {
      setPreview(await contextPreviewApi(api, conversationId).read(profileId));
    } catch {
      setPreview(null);
    }
  };
  useEffect(() => {
    if (!expanded) return;
    void refreshPreview();
  }, [expanded, conversationId, events, profileId]);

  const act = async (
    eventId: string,
    operation: () => Promise<unknown>,
    message: string,
  ): Promise<WorkEvent | undefined> => {
    if (busyId) return;
    setBusyId(eventId);
    setError("");
    setStatus("");
    try {
      const value = await operation();
      const merged = asWorkEvent(value);
      if (merged) patch(merged);
      if (message) setStatus(message);
      return merged;
    } catch (cause) {
      setError(errorMessage(cause));
      return undefined;
    } finally {
      setBusyId("");
    }
  };

  const start = async () => {
    if (starting || disabled || !title.trim()) return;
    setStarting(true);
    setError("");
    setStatus("");
    try {
      const value = await client!.start({
        title: title.trim(),
        request_id: requestId(),
        ...(startAt ? { start_message_id: startAt } : {}),
      });
      setTitle("");
      setStartAt("");
      setEvents((current) =>
        current
          ? [...current.filter((item) => item.id !== value.id), value]
          : [value],
      );
      setStatus(
        startAt
          ? "已按你选定的起点建立工作事件；结束工作后会按覆盖情况折叠。"
          : "已开始记录这段工作；结束工作后会按覆盖情况折叠。",
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setStarting(false);
    }
  };

  const jobsFor = async (eventId: string) => {
    try {
      if (client) setJobs(await client.jobs(eventId));
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const cancelJob = async (eventId: string, jobId: string) => {
    setError("");
    setStatus("");
    try {
      await client!.cancelJob(eventId, jobId);
      setStatus("已请求取消这次概况生成；已完成的调用不会被退回。");
    } catch (cause) {
      setError(errorMessage(cause));
    }
    await jobsFor(eventId);
    // 作业被取消或启用后会改变事件版本，列表要跟上真实状态。
    await load();
  };

  const list = events ?? [];
  const openEvent = list.find(
    (item) => (item.lifecycle ?? item.status) === "open",
  );
  const stale = list.filter((item) => item.status === "stale").length;
  const head = openEvent
    ? "进行中：" + (openEvent.title || "未命名工作")
    : list.length
      ? list.length + " 段工作记录"
      : "还没有工作记录";

  return (
    <section className="work-panel" aria-label="工作事件">
      <div className="work-panel-head">
        <button
          type="button"
          className="work-panel-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="work-panel-title">工作事件</span>
          <span className="work-panel-summary metadata">{head}</span>
          {stale > 0 && <span className="pill warm">{stale} 段来源已失效</span>}
          {openEvent && <span className="pill">进行中</span>}
        </button>
        <button
          type="button"
          className="text-button"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? "读取中…" : "刷新"}
        </button>
      </div>
      {expanded && (
        <div className="work-panel-body">
          <p className="metadata">
            工作事件只标记一段明确的工作边界。折叠是给模型输入用的：折叠后后续请求
            里用短概况代替这段工作的细节；下面的「查看记录」是你自己回读原文，不发
            送给模型，也不会改变原文和执行状态。
          </p>
          {(error || work.error) && (
            <p className="notice error" role="alert">
              {error || work.error}
            </p>
          )}
          {status && (
            <p className="management-status" role="status">
              {status}
            </p>
          )}
          <ContextEstimate preview={preview} />
          {!openEvent && (
            <form
              className="work-start"
              onSubmit={(event) => {
                event.preventDefault();
                void start();
              }}
            >
              <label htmlFor={"work-title-" + conversationId}>
                这次要做什么（一句话）
              </label>
              <input
                id={"work-title-" + conversationId}
                value={title}
                maxLength={500}
                required
                autoComplete="off"
                disabled={disabled || starting}
                placeholder="例如：修复登录后返回空白的故障"
                onChange={(event) => setTitle(event.target.value)}
              />
              <label htmlFor={"work-start-" + conversationId}>
                工作起点（可选）
              </label>
              <select
                id={"work-start-" + conversationId}
                value={startAt}
                disabled={disabled || starting}
                onChange={(event) => setStartAt(event.target.value)}
              >
                <option value="">从现在之后开始记录</option>
                {messages
                  .filter((message) => message.role === "user")
                  .map((message) => (
                    <option key={message.id} value={message.id}>
                      从「{message.text.slice(0, 30) || "（空消息）"}
                      」开始（到当前 为止）
                    </option>
                  ))}
              </select>
              <p className="field-help">
                不选起点时，本事件只覆盖现在之后的新消息；选择历史消息表示这一整段到
                当前进度都算这次工作，结束时会按当前分支核对。
              </p>
              <button
                className="button secondary"
                disabled={disabled || starting || !title.trim()}
              >
                {starting ? "正在开始…" : "开始工作"}
              </button>
            </form>
          )}
          {list.length === 0 && !loading && (
            <p className="muted small">这段对话还没有工作事件。</p>
          )}
          <ul className="work-events" aria-label="工作事件列表">
            {list.map((event) => (
              <EventCard
                key={event.id}
                api={api}
                agentId={agentId}
                event={event}
                client={client!}
                busy={busyId === event.id || disabled}
                expanded={openId === event.id}
                onExpand={() => setOpenId(openId === event.id ? "" : event.id)}
                settings={settings}
                jobs={jobsId === event.id ? jobs : []}
                onJobs={() => {
                  setJobsId(event.id);
                  void jobsFor(event.id);
                }}
                onCancelJob={async (jobId) => {
                  await cancelJob(event.id, jobId);
                }}
                onLink={(value, text) => {
                  setEvents((current) =>
                    current
                      ? current.map((item) =>
                          item.id === value.id ? value : item,
                        )
                      : [value],
                  );
                  setStatus(text);
                }}
                onClose={() =>
                  void act(
                    event.id,
                    () => client!.close(event.id, event.revision),
                    "已结束这段工作；是否折叠已按来源覆盖由服务端判定。",
                  )
                }
                onFold={(folded) =>
                  void act(
                    event.id,
                    () => client!.fold(event.id, event.revision, folded),
                    folded
                      ? "已折叠到模型输入：后续请求改用短概况。"
                      : "已恢复原始输入：后续请求重新携带这段工作的消息。",
                  )
                }
                onSummary={async (text) => {
                  const value = await act(
                    event.id,
                    () =>
                      client!.correctSummary(event.id, event.revision, text),
                    "概况已更正（标记为你的更正）。",
                  );
                  return !!value;
                }}
                onSummarize={async () => {
                  setError("");
                  setStatus("");
                  try {
                    await client!.summarize(event.id, event.revision);
                    setStatus(
                      "已请求用你选择的模型生成概况；正文属于未核验文本。",
                    );
                  } catch (cause) {
                    setError(errorMessage(cause));
                  }
                  setJobsId(event.id);
                  await jobsFor(event.id);
                }}
              />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function EventCard({
  api,
  agentId,
  event,
  client,
  busy,
  expanded,
  onExpand,
  onClose,
  onFold,
  onSummary,
  onSummarize,
  onLink,
  onCancelJob,
  settings,
  jobs,
  onJobs,
}: {
  api: LingApi;
  agentId: string;
  event: WorkEvent;
  client: WorkEventsClient;
  busy: boolean;
  expanded: boolean;
  onExpand: () => void;
  onClose: () => void;
  onFold: (folded: boolean) => void;
  onSummary: (text: string) => Promise<boolean>;
  onSummarize: () => Promise<void>;
  onLink: (value: WorkEvent, text: string) => void;
  onCancelJob: (jobId: string) => Promise<void>;
  settings: WorkEventSettings | null;
  jobs: WorkEventSummaryJob[];
  onJobs: () => void;
}) {
  const [draft, setDraft] = useState(event.summary ?? "");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const lifecycle = event.lifecycle ?? event.status;
  const open = lifecycle === "open";
  const invalid = event.valid === false;
  const tone = outcomeTone(event.outcome);

  return (
    <li className={"work-event" + (invalid ? " invalid" : "")}>
      <div className="work-event-head">
        <h3 className="work-event-title">
          {invalid ? "来源已失效的工作" : event.title || "未命名工作"}
        </h3>
        <span
          className={
            "pill" +
            (open ? "" : tone === "warn" ? " warm" : tone === "ok" ? " ok" : "")
          }
        >
          {lifecycleLabel(event)}
          {!open && " · " + outcomeLabel(event.outcome)}
        </span>
        {!open && <span className="pill">{coverageLabel(event.coverage)}</span>}
        {typeof event.source_count === "number" && (
          <span className="metadata">{event.source_count} 条来源</span>
        )}
      </div>
      {invalid && (
        <p className="notice error" role="alert">
          {reasonLabel(event.reason) ||
            "这段工作的来源或分支已经改变，概况与折叠都不再生效。"}
        </p>
      )}
      {!invalid && !open && (
        <p className="metadata">
          模型输入：
          {event.folded ? "已折叠为概况" : "保留原始输入视图，仍受窗口限制"}
          {event.summary_kind
            ? " · " + summaryKindLabel(event.summary_kind)
            : ""}
        </p>
      )}
      {!invalid && hasGap(event.coverage) && (
        <p className="notice" role="status">
          这段工作只保留了部分来源（{coverageLabel(event.coverage)}
          ），界面只能展示实际留存的内容，不能宣称原文完整。
        </p>
      )}
      {!invalid && !open && event.summary && (
        <p className="preserve-text work-event-summary">{event.summary}</p>
      )}
      {!invalid && !open && !event.summary && (
        <p className="muted small">
          还没有概况文本；折叠到模型输入前建议先确认概况。
        </p>
      )}
      <div className="work-event-actions">
        {open && (
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={onClose}
          >
            {busy ? "正在结束…" : "结束工作"}
          </button>
        )}
        {!invalid && !open && (
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() => onFold(!event.folded)}
          >
            {event.folded ? "恢复原始输入" : "折叠到模型输入"}
          </button>
        )}
        {!invalid && !open && (
          <button
            type="button"
            className="text-button"
            aria-expanded={expanded}
            onClick={onExpand}
          >
            {expanded ? "收起记录" : "查看记录"}
          </button>
        )}
        {!invalid && !open && (
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setDraft(event.summary ?? "");
              setEditing(!editing);
              setFailed(false);
            }}
          >
            {editing ? "取消更正" : "更正概况"}
          </button>
        )}
        {!invalid && !open && settings?.enabled && settings.profile_id && (
          <>
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() => void onSummarize()}
            >
              用所选模型生成概况
            </button>
            <button type="button" className="text-button" onClick={onJobs}>
              查看生成状态
            </button>
            <p className="metadata">
              概况由你选择的模型生成，属于未核验文本；不会自动覆盖你的更正，也不会
              由它决定执行结果。
            </p>
          </>
        )}
      </div>
      {editing && (
        <form
          className="work-summary-edit"
          onSubmit={(formEvent) => {
            formEvent.preventDefault();
            if (saving || !draft.trim()) return;
            setSaving(true);
            setFailed(false);
            void onSummary(draft.trim()).then((saved) => {
              setSaving(false);
              if (saved) setEditing(false);
              else setFailed(true);
            });
          }}
        >
          <label htmlFor={"work-summary-" + event.id}>概况文本</label>
          <textarea
            id={"work-summary-" + event.id}
            value={draft}
            maxLength={2048}
            disabled={saving}
            onChange={(inputEvent) => setDraft(inputEvent.target.value)}
          />
          <p className="field-help">
            概况只描述已经核实的状态；执行结果仍以运行记录为准，这里写下的文字不会
            改变任何执行状态。
            {new TextEncoder().encode(draft).length} / 2048 字节。
          </p>
          <div className="button-row">
            <button
              className="button secondary"
              disabled={
                saving ||
                busy ||
                !draft.trim() ||
                new TextEncoder().encode(draft).length > 2048
              }
            >
              {saving ? "正在保存…" : "保存更正"}
            </button>
          </div>
          {failed && (
            <p className="metadata">
              更正未保存。若提示版本冲突，请先刷新列表再基于最新版本修改。
            </p>
          )}
        </form>
      )}
      {jobs.length > 0 && (
        <ul className="work-jobs" aria-label="概况生成状态">
          {jobs.map((job) => (
            <li key={job.id} className="work-job">
              <span>{summaryJobLabel(job.status, job.outcome)}</span>
              <span className="metadata">
                {job.profile_id ? "连接：" + job.profile_id + " · " : ""}
                {job.max_calls ? "最多 " + job.max_calls + " 次调用 · " : ""}
                {usageText(job.usage)}
                {job.coverage ? " · 覆盖：" + coverageLabel(job.coverage) : ""}
                {typeof job.event_revision === "number"
                  ? " · 版本 " + job.event_revision
                  : ""}
              </span>
              {job.error_code && (
                <span className="notice error" role="alert">
                  生成未完成：{jobErrorLabel(job.error_code)}
                </span>
              )}
              {job.detail && <span className="metadata">{job.detail}</span>}
              {job.status === "done" && job.outcome === "applied" && (
                <span className="notice success" role="status">
                  已启用为这段工作的概况。文本未经人工核验，执行结果仍以运行记录为准。
                </span>
              )}
              {job.status === "done" && job.outcome === "rejected" && (
                <span className="notice" role="status">
                  生成的文本未通过校验，概况保持原样；原文与执行状态都没有改变。
                </span>
              )}
              {job.status === "done" && job.outcome === "rule_only" && (
                <span className="notice" role="status">
                  模型没有给出可用概况，当前只保留规则概况。
                </span>
              )}
              {job.status === "stale" && (
                <span className="metadata">
                  这段工作在上次生成后已变化，需要重新生成概况。
                </span>
              )}
              {jobRunning(job.status) && (
                <button
                  type="button"
                  className="text-button danger"
                  disabled={!!job.cancel_requested}
                  onClick={() => void onCancelJob(job.id)}
                >
                  {job.cancel_requested ? "已请求取消" : "取消生成"}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {expanded && (
        <EventRecords
          client={client}
          eventId={event.id}
          coverage={event.coverage}
        />
      )}
      {open && (
        <div className="work-event-linkrow">
          <EventTaskLink
            api={api}
            agentId={agentId}
            client={client}
            eventId={event.id}
            revision={event.revision}
            busy={busy}
            onLinked={(value, text) => onLink(value, text)}
          />
        </div>
      )}
    </li>
  );
}
