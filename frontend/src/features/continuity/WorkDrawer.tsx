import { useEffect, useRef, useState } from "react";
import {
  Archive,
  ChevronRight,
  CircleStop,
  Pause,
  Play,
  Send,
  X,
} from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent, WorkBranch } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice } from "../../shared/ui";
import {
  continuityRequestId,
  workStatusLabel,
  workCanFinish,
} from "./api";
import type { WorkBranchesState } from "./useContinuation";

type Filter = "active" | "archive";

function age(work: WorkBranch) {
  const stamp = work.finished_at || work.created_at;
  if (!stamp) return "时间未记录";
  const date = new Date(stamp);
  return Number.isNaN(date.getTime())
    ? "时间未记录"
    : new Intl.DateTimeFormat("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
}

function branchStateLabel(work: WorkBranch) {
  if (work.return_state === "published") return "摘要已回归主干";
  if (["archived", "completed"].includes(work.status)) return "已归档";
  if (work.status === "failed") return "失败，可回查";
  if (work.status === "unknown") return "结果不确定";
  return workStatusLabel(work.status);
}

function BranchDetails({
  api,
  work,
  state,
  onClose,
}: {
  api: LingApi;
  work: WorkBranch;
  state: WorkBranchesState;
  onClose: () => void;
}) {
  const [guide, setGuide] = useState("");
  const [status, setStatus] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [localError, setLocalError] = useState("");
  const [pausing, setPausing] = useState(false);
  const [finishing, setFinishing] = useState(false);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const applyPaused = async (resume: boolean) => {
    if (pausing || !work.run_id) return;
    setPausing(true);
    setLocalError("");
    try {
      if (resume) await api.resumeChat(work.run_id);
      else await api.pauseChat(work.run_id);
      if (alive.current)
        setStatus(resume ? "已请求继续，等待运行响应。" : "已请求暂停，等待运行到达安全步骤边界。");
    } catch (cause) {
      if (alive.current) setLocalError(errorMessage(cause));
    } finally {
      if (alive.current) setPausing(false);
    }
  };

  const sendGuide = async () => {
    const text = guide.trim();
    if (!text) return;
    setLocalError("");
    setStatus("");
    const value = await state.guide(work.id, text, continuityRequestId());
    if (value) {
      setGuide("");
      setStatus("指导已交给工作分支；来源和时间会随分支归档保留。");
    }
  };

  const finish = async () => {
    if (finishing) return;
    setFinishing(true);
    setLocalError("");
    const value = await state.finish(work.id, work.revision);
    setFinishing(false);
    if (value)
      setStatus(
        value.status === "completed"
          ? "工作已经结束；只有服务端核验过交付后，摘要才会回归主干。"
          : "完成意图已记录，仍在核对执行与交付状态。",
      );
  };

  const cancel = async () => {
    setConfirmCancel(false);
    const value = await state.cancel(work.id, work.revision);
    if (value) setStatus("已请求取消；未知副作用仍保留核对入口，不会自动解除。");
  };

  return (
    <section className="continuity-branch-detail" aria-label="工作分支详情">
      <div className="continuity-branch-detail-head">
        <div>
          <span className="eyebrow">独立工作分支</span>
          <h3>{work.objective}</h3>
        </div>
        <button className="icon-button" aria-label="返回工作列表" onClick={onClose}>
          <ChevronRight size={17} className="continuity-back-arrow" aria-hidden="true" />
        </button>
      </div>
      <div className="continuity-status-row">
        <span className={`pill ${work.status === "unknown" ? "warm" : ""}`}>
          {branchStateLabel(work)}
        </span>
        <span className="metadata">{age(work)} · 版本 {work.revision}</span>
      </div>
      {work.outcome && <p className="metadata">结果：{work.outcome}</p>}
      {work.delivery_state && <p className="metadata">交付：{work.delivery_state}</p>}
      {work.return_state && <p className="metadata">回归：{work.return_state}</p>}

      <div className="continuity-branch-actions">
        {work.run_id && ["running", "paused", "waiting", "finishing"].includes(work.status) && (
          <>
            <button className="button secondary small-button" disabled={pausing} onClick={() => void applyPaused(false)}>
              <Pause size={14} />请求暂停
            </button>
            <button className="button secondary small-button" disabled={pausing} onClick={() => void applyPaused(true)}>
              <Play size={14} />请求继续
            </button>
          </>
        )}
        {!["archived", "completed", "cancelled", "failed"].includes(work.status) && (
          <button
            className="button secondary small-button"
            disabled={state.busyId === work.id}
            onClick={() => setConfirmCancel(true)}
          >
            <CircleStop size={14} />取消工作
          </button>
        )}
        {workCanFinish(work) && (
          <button
            className="button primary small-button"
            disabled={finishing || state.busyId === work.id}
            onClick={() => void finish()}
          >
            <Archive size={14} />
            {finishing ? "正在核对…" : "完成并归档"}
          </button>
        )}
      </div>

      {(localError || state.actionError) && (
        <ErrorNotice message={localError || state.actionError} />
      )}
      {status && <p className="notice success" role="status">{status}</p>}

      <form
        className="continuity-guide"
        onSubmit={(event) => {
          event.preventDefault();
          void sendGuide();
        }}
      >
        <label htmlFor={`work-guide-${work.id}`}>给正在工作的一支补充指导</label>
        <textarea
          id={`work-guide-${work.id}`}
          value={guide}
          maxLength={4000}
          placeholder="例如：先核对现有测试，再修登录失败；不要改动生产数据。"
          onChange={(event) => setGuide(event.target.value)}
        />
        <button className="button secondary small-button" disabled={!guide.trim() || !!state.busyId || ["archived", "completed", "failed", "cancelled"].includes(work.status)}>
          <Send size={14} />发送指导
        </button>
      </form>
      <p className="metadata">
        这里发送的是工作分支指导。查看归档内容只读本地记录，不会调用模型。
      </p>
      {confirmCancel && (
        <ConfirmDialog
          title="取消这支工作？"
          confirmLabel="确认取消"
          busy={state.busyId === work.id}
          onCancel={() => setConfirmCancel(false)}
          onConfirm={() => void cancel()}
        >
          <p>
            已确认的操作不会被回滚。若命令、工具或文件交付尚不确定，灵会保留恢复与核对入口；
            原分支记录和归档都会保留。
          </p>
        </ConfirmDialog>
      )}
    </section>
  );
}

export function WorkDrawer({
  api,
  state,
  mainConversationId,
  onOpenConversation,
  onOpenExperience,
  onClose,
}: {
  api: LingApi;
  agent?: Agent;
  state: WorkBranchesState;
  mainConversationId: string | null;
  onOpenConversation: (id: string) => void;
  onOpenExperience: (id: string) => void;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState<Filter>("active");
  const [selected, setSelected] = useState("");
  const [starting, setStarting] = useState(false);
  const [objective, setObjective] = useState("");
  const [notice, setNotice] = useState("");
  const running = state.items.filter(
    (item) => !["archived", "completed", "cancelled", "failed"].includes(item.status),
  );
  const archived = state.items.filter((item) =>
    ["archived", "completed", "cancelled", "failed"].includes(item.status),
  );
  const list = filter === "active" ? running : archived;
  const branch = state.items.find((item) => item.id === selected);

  const start = async () => {
    const text = objective.trim();
    if (!text || starting || !mainConversationId) return;
    setStarting(true);
    setNotice("");
    const value = await state.start({
      request_id: continuityRequestId(),
      objective: text,
    });
    setStarting(false);
    if (value) {
      setObjective("");
      setSelected(value.id);
      setNotice(
        value.status === "queued"
          ? "工作已排队；同一时间主干聊天仍可继续。"
          : "工作分支已开始，主干保持独立。",
      );
    }
  };

  return (
    <aside className="continuity-drawer" aria-label="工作与经历抽屉">
      <div className="continuity-drawer-head">
        <div>
          <p className="eyebrow">工作抽屉</p>
          <h2>{branch ? "查看工作" : "让灵独立做完一件事"}</h2>
        </div>
        <button className="icon-button" aria-label="关闭工作抽屉" onClick={onClose}>
          <X size={17} aria-hidden="true" />
        </button>
      </div>
      <div className="continuity-drawer-scroll">
        {branch ? (
          <BranchDetails
            key={branch.id}
            api={api}
            work={branch}
            state={state}
            onClose={() => setSelected("")}
          />
        ) : (
          <>
            <form
              className="continuity-work-start"
              onSubmit={(event) => {
                event.preventDefault();
                void start();
              }}
            >
              <label htmlFor="continuity-objective">这次要完成什么</label>
              <textarea
                id="continuity-objective"
                value={objective}
                maxLength={2000}
                required
                placeholder="例如：修好设置页刷新后丢失时间的问题，并通过现有测试。"
                onChange={(event) => setObjective(event.target.value)}
              />
              <p className="field-help">
                会从当前主干冻结必要背景。工作过程只留在独立分支；默认一支执行，其余排队，主干仍可继续聊天。
              </p>
              <button className="button primary" disabled={!objective.trim() || starting || !mainConversationId}>
                {starting ? "正在创建…" : "开始工作"}
              </button>
            </form>
            {!mainConversationId && <p className="notice warm">先在对话页选择一个主干，才能开始工作。</p>}
            {notice && <p className="notice success" role="status">{notice}</p>}
            {(state.error || state.actionError) && (
              <ErrorNotice message={state.error || state.actionError} retry={() => void state.refresh()} />
            )}
            <div className="continuity-tabs" role="tablist" aria-label="工作分支状态">
              <button
                role="tab"
                aria-selected={filter === "active"}
                className={filter === "active" ? "active" : ""}
                onClick={() => setFilter("active")}
              >
                进行与排队 {running.length}
              </button>
              <button
                role="tab"
                aria-selected={filter === "archive"}
                className={filter === "archive" ? "active" : ""}
                onClick={() => setFilter("archive")}
              >
                已结束 {archived.length}
              </button>
            </div>
            {state.status === "loading" && state.items.length === 0 && (
              <p className="continuity-loading" role="status">正在读取工作分支…</p>
            )}
            {list.length === 0 && (
              <p className="continuity-empty">
                {filter === "active" ? "现在没有进行或排队的工作。" : "结束的工作会保留在这里，可查看或从经历页回查原文。"}
              </p>
            )}
            <ul className="continuity-work-list">
              {list.map((item) => (
                <li key={item.id}>
                  <button className="continuity-work-row" onClick={() => setSelected(item.id)}>
                    <span className="continuity-work-row-main">
                      <strong>{item.objective}</strong>
                      <span className="metadata">
                        {branchStateLabel(item)} · {age(item)}
                      </span>
                    </span>
                    <ChevronRight size={15} aria-hidden="true" />
                  </button>
                  <div className="continuity-work-row-actions">
                    {(
                      <button className="text-button" onClick={() => onOpenConversation(item.conversation_id)}>
                        切换查看
                      </button>
                    )}
                    {["archived", "completed"].includes(item.status) && item.archive_id && (
                      <button className="text-button" onClick={() => onOpenExperience(item.archive_id!)}>
                        查看归档与原文
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </aside>
  );
}

export type { WorkBranch };
