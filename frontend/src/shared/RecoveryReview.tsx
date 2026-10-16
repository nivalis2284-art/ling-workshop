import { useRef, useState } from "react";
import { ApiError, errorMessage } from "../api/client";
import type { LingApi } from "../api/client";
import { ConfirmDialog } from "./ConfirmDialog";
import { ErrorNotice, formatDate } from "./ui";

export type RecoveryKind = "chat" | "task";
export type RecoveryConclusion = "reviewed" | "unresolved" | "abandon";

export interface RecoverySession {
  session_id: string;
  status: string;
  error?: string;
}

export interface RecoveryEffect {
  step?: number;
  call_id?: string;
  id?: number | string;
  status?: string;
}

export interface RecoveryRecord {
  request_id: string;
  conclusion: string;
  note: string;
  actor: string;
  created_at: string;
  cleanup_confirmed?: boolean;
  observed_version?: string;
}

export interface RecoveryView {
  run_id: string;
  kind: RecoveryKind;
  status: string;
  sessions: RecoverySession[];
  uncertain_effects: RecoveryEffect[];
  reviews: RecoveryRecord[];
  can_abandon: boolean;
  guidance: string;
  version: string;
  workspace_policy?: {max_files: number; max_bytes: number; max_file_bytes: number; excluded: string[]};
}

const conclusions: Record<string, string> = {
  reviewed: "已人工核对",
  unresolved: "仍未解决",
  abandon: "放弃恢复旧运行",
};

const sessionStates: Record<string, string> = {
  running: "仍在运行",
  cancelling: "正在停止",
  success: "已正常退出",
  failed: "执行失败",
  timeout: "执行超时",
  cancelled: "已取消",
  unknown: "状态未知",
};
const runStates: Record<string, string> = {
  running: "执行中", queued: "等待执行", blocked: "需要处理", interrupted: "已中断",
  failed: "运行失败", paused: "已暂停", waiting_owner: "等待确认", completed: "已结束",
  needs_review: "结果待核对", cancelled: "已取消",
};

function requestId() {
  // crypto.randomUUID 在旧环境缺失时退回本地唯一值，不影响请求语义。
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
    return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function RecoveryReview({
  api,
  runId,
  kind,
  onUpdated,
}: {
  api: LingApi;
  runId: string;
  kind: RecoveryKind;
  onUpdated?: () => Promise<void>;
}) {
  const [view, setView] = useState<RecoveryView | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmAbandon, setConfirmAbandon] = useState(false);
  const locked = useRef(false);
  const path = `/execution/recovery/${kind}/${encodeURIComponent(runId)}`;

  const load = async () => {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      setView(await api.call<RecoveryView>(path));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };

  const record = async (conclusion: RecoveryConclusion) => {
    if (locked.current || !view) return;
    const text = note.trim();
    if (text.length < 1 || text.length > 2000) {
      setError("请填写 1–2000 字的核对备注，说明你实际核对了什么。");
      return;
    }
    locked.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const next = await api.call<RecoveryView>(path, {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId(),
          expected_version: view.version,
          conclusion,
          note: text,
        }),
      });
      setView(next);
      setNote("");
      setMessage(
        conclusion === "abandon"
          ? "已放弃恢复这次旧运行。它不会再被恢复；这不会清理仍在运行的进程。"
          : conclusion === "reviewed"
            ? "已记录这次人工核对。记录不会解除未知副作用的阻断，也不证明进程已停止。"
            : "已记录仍未解决。请先处置不确定的副作用，再重新核对。",
      );
      await onUpdated?.();
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 409
          ? "核对记录已发生变化，已重新读取最新状态，请再次确认。" +
              errorMessage(cause)
          : errorMessage(cause),
      );
      if (cause instanceof ApiError && cause.status === 409) {
        try {
          setView(await api.call<RecoveryView>(path));
        } catch {
          /* 重新读取失败时保留原视图，用户可手动刷新。 */
        }
      }
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };

  const pending = view?.reviews ?? [];
  return (
    <section className="stack" aria-label="恢复与核对">
      <h3>恢复核对</h3>
      <p className="muted small">
        这里的记录只是你的人工判断：它不会解除未知副作用的阻断，也不证明相关命令或进程已经停止。
        「放弃」只停止恢复这次旧运行，不会清理进程；程序是否确认清理单独标注。
      </p>
      {!view && (
        <button
          className="button secondary"
          disabled={busy}
          onClick={() => void load()}
        >
          读取恢复记录与证据
        </button>
      )}
      {error && <ErrorNotice message={error} />}
      {message && (
        <p className="management-status" role="status">
          {message}
        </p>
      )}
      {view && (
        <div className="stack">
          <p className="metadata">
            运行状态：{runStates[view.status] ?? "需要核对"} · 是否允许放弃：{view.can_abandon ? "允许" : "不允许"}
          </p>
          <p className="muted small">{view.guidance}</p>
          {view.workspace_policy && (
            <details>
              <summary>工作区检查范围与上限</summary>
              <p className="metadata">最多 {view.workspace_policy.max_files} 个文件，
                总计 {view.workspace_policy.max_bytes / 1048576} MiB，
                单文件 {view.workspace_policy.max_file_bytes / 1048576} MiB。
                依赖、缓存和 .env 文件不纳入检查；超出范围时请选取更小的工作目录。</p>
              <p className="metadata">排除目录：{view.workspace_policy.excluded.join("、")}</p>
            </details>
          )}
          <section>
            <h4>证据引用：进程会话</h4>
            {view.sessions.length === 0 ? (
              <p className="muted small">
                服务没有返回仍在跟踪的进程会话，这不等同于已确认清理。
              </p>
            ) : (
              <ul>
                {view.sessions.map((item) => (
                  <li key={item.session_id}>
                    {item.session_id} ·{" "}
                    {sessionStates[item.status] ?? item.status}
                    {item.error ? `（${item.error}）` : ""}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section>
            <h4>证据引用：不确定副作用</h4>
            {view.uncertain_effects.length === 0 ? (
              <p className="muted small">服务没有列出不确定副作用的记录。</p>
            ) : (
              <ul>
                {view.uncertain_effects.map((item, index) => (
                  <li key={`${item.call_id ?? item.id ?? index}`}>
                    {item.call_id ?? item.id ?? "未命名步骤"} ·{" "}
                    {item.status ?? "状态未知"}
                    {item.step !== undefined ? `（第 ${item.step} 步）` : ""}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section>
            <h4>最近核对记录</h4>
            {pending.length === 0 ? (
              <p className="muted small">还没有任何人工核对记录。</p>
            ) : (
              <ul>
                {pending.map((item) => (
                  <li key={item.request_id}>
                    {conclusions[item.conclusion] ?? item.conclusion} ·{" "}
                    {item.actor === "owner" ? "所有者" : "需核对来源"} · {formatDate(item.created_at)} ·{" "}
                    {item.cleanup_confirmed
                      ? "程序已确认清理"
                      : "程序未确认清理"}
                    ：{item.note}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <label className="metadata">
            备注（1–2000 字）
            <textarea
              value={note}
              maxLength={2000}
              disabled={busy}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <div className="button-row">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void record("reviewed")}
            >
              记录为已人工核对
            </button>
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void record("unresolved")}
            >
              记录为仍未解决
            </button>
            <button
              className="button danger"
              disabled={busy || !view.can_abandon}
              onClick={() => setConfirmAbandon(true)}
            >
              放弃恢复旧运行
            </button>
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void load()}
            >
              重新读取
            </button>
          </div>
          {!view.can_abandon && (
            <p className="muted small">
              放弃只适用于已中断或受阻的旧运行；正在执行的运行请先停止，已经结束的运行可保留核对记录。
            </p>
          )}
        </div>
      )}
      {confirmAbandon && view && (
        <ConfirmDialog
          title="放弃恢复这次旧运行？"
          confirmLabel="确认放弃"
          busy={busy}
          onCancel={() => setConfirmAbandon(false)}
          onConfirm={() => {
            void record("abandon").then(() => setConfirmAbandon(false));
          }}
        >
          <p>
            放弃后，这次旧运行不会再被恢复，未知副作用的阻断也仍然存在。
          </p>
          <p>
            这只停止恢复旧运行，不会清理任何仍在运行的进程，也不等于已确认清理。
            请按证据逐项核对你关心的副作用。
          </p>
          {error && <ErrorNotice message={error} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
