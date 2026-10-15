import { useState } from "react";
import type { LingApi } from "../../api/client";
import type { ExecutionSession } from "../../api/types";
import type { TaskEvent } from "./api";
import { EmptyState, formatDate } from "../../shared/ui";
import { RecordData } from "./ManagementUi";

const labels: Record<string, string> = {
  status: "状态变化",
  model_started: "开始处理",
  usage: "服务用量记录",
  plan: "处理思路",
  tool_started: "工具开始执行",
  tool_result: "工具执行结果",
  final: "最终答复",
  applied: "文件改动已应用",
};
function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function eventSession(event: TaskEvent): ExecutionSession | null {
  if (event.type !== "tool_result") return null;
  const execution = record(record(event.data.result)?.execution);
  if (
    !execution ||
    typeof execution.session_id !== "string" ||
    typeof execution.status !== "string" ||
    typeof execution.cursor !== "number"
  )
    return null;
  return execution as unknown as ExecutionSession;
}

export function TaskEvents({
  events,
  api,
  taskId,
}: {
  events: TaskEvent[];
  api: LingApi;
  taskId: string;
}) {
  const [sessions, setSessions] = useState<Record<string, ExecutionSession>>({});
  const [busy, setBusy] = useState<Record<string, "refreshing" | "cancelling">>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const updateSession = async (
    initial: ExecutionSession,
    cancel = false,
  ) => {
    const id = initial.session_id;
    const current = sessions[id] ?? initial;
    setBusy((value) => ({ ...value, [id]: cancel ? "cancelling" : "refreshing" }));
    setErrors((value) => ({ ...value, [id]: "" }));
    try {
      const next = cancel
        ? await api.cancelExecutionSession(id, taskId, "task")
        : await api.executionSession(id, taskId, "task", current.cursor);
      setSessions((value) => {
        const previous = value[id] ?? initial;
        return {
          ...value,
          [id]: {
            ...previous,
            ...next,
            output: cancel
              ? next.output ?? previous.output
              : (previous.output ?? "") + (next.output ?? ""),
          },
        };
      });
    } catch (error) {
      setErrors((value) => ({
        ...value,
        [id]: error instanceof Error ? error.message : "读取命令会话失败。",
      }));
    } finally {
      setBusy((value) => {
        const next = { ...value };
        delete next[id];
        return next;
      });
    }
  };

  return (
    <section aria-label="任务执行记录">
      <details>
        <summary>实际执行记录（{events.length} 条，点击展开）</summary>
        {events.length === 0 ? (
          <EmptyState title="还没有执行记录">
            这里会展示服务已经记录的事件。
          </EmptyState>
        ) : (
          <ol className="management-list">
            {events.map((event) => (
              <li key={event.id} className="panel management-card">
                <div className="section-heading">
                  <strong>
                    {labels[event.type] ?? "执行记录：" + event.type}
                  </strong>
                  <time className="metadata">
                    {formatDate(event.created_at)}
                  </time>
                </div>
                {typeof event.data.text === "string" && (
                  <p className="management-copy">{event.data.text}</p>
                )}
                {typeof event.data.name === "string" && (
                  <p className="metadata">工具：{event.data.name}</p>
                )}
                {(() => {
                  const initial = eventSession(event);
                  if (!initial) return null;
                  const session = sessions[initial.session_id] ?? initial;
                  const action = busy[initial.session_id];
                  const active = ["running", "cancelling"].includes(session.status);
                  return (
                    <section aria-label="命令会话" className="stack">
                      <p className="management-status">
                        命令状态：{session.status}
                        {typeof session.result?.exit_code === "number" &&
                          ` · 退出码 ${session.result.exit_code}`}
                      </p>
                      {session.output && (
                        <pre className="management-copy" aria-label="命令输出">
                          {session.output}
                        </pre>
                      )}
                      {session.truncated && (
                        <p className="metadata">输出已截断。</p>
                      )}
                      <div className="button-row">
                        <button
                          type="button"
                          className="button secondary"
                          disabled={Boolean(action)}
                          onClick={() => void updateSession(initial)}
                        >
                          {action === "refreshing" ? "刷新中…" : "刷新命令状态"}
                        </button>
                        {active && session.status === "running" && (
                          <button
                            type="button"
                            className="button secondary"
                            disabled={Boolean(action)}
                            onClick={() => void updateSession(initial, true)}
                          >
                            {action === "cancelling" ? "正在停止…" : "停止命令"}
                          </button>
                        )}
                      </div>
                      {errors[initial.session_id] && (
                        <p className="inline-warning">{errors[initial.session_id]}</p>
                      )}
                    </section>
                  );
                })()}
                <RecordData value={event.data} />
              </li>
            ))}
          </ol>
        )}
      </details>
    </section>
  );
}
