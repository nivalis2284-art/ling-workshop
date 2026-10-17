import { useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import { taskLabels, tasksApi } from "../tasks/api";
import type { Task } from "../tasks/api";
import type { WorkEvent, WorkEventsClient } from "./api";

/**
 * 把进行中的工作与已有任务显式关联：必须由所有者在这里挑选，同个体或时间
 * 相近都不构成关联。关联不会改变任务自身的执行状态。
 */
export function EventTaskLink({
  api,
  agentId,
  eventId,
  revision,
  busy,
  client,
  onLinked,
}: {
  api: LingApi;
  agentId: string;
  eventId: string;
  revision: number;
  busy: boolean;
  client: WorkEventsClient;
  onLinked: (value: WorkEvent, text: string) => void;
}) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [taskId, setTaskId] = useState("");
  const [loading, setLoading] = useState(false);
  const [linking, setLinking] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    if (loading) return;
    setLoading(true);
    setError("");
    try {
      const controller = new AbortController();
      setTasks(await tasksApi(api).list(agentId, controller.signal));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  };

  const link = async (client: WorkEventsClient) => {
    if (!taskId || linking || busy) return;
    setLinking(true);
    setError("");
    try {
      const value = await client.linkTask(eventId, {
        task_id: taskId,
        expected_revision: revision,
      });
      setTaskId("");
      onLinked(value, "已把选中的任务关联到这段工作。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLinking(false);
    }
  };

  return (
    <div className="work-task-link">
      {!tasks ? (
        <button
          type="button"
          className="text-button"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? "正在读取任务…" : "关联已有任务"}
        </button>
      ) : tasks.length === 0 ? (
        <p className="metadata">
          这个灵还没有任务；可以先在任务页创建，再回到这里关联。
        </p>
      ) : (
        <>
          <label htmlFor={"work-task-" + eventId}>关联已有任务</label>
          <select
            id={"work-task-" + eventId}
            value={taskId}
            disabled={linking || busy}
            onChange={(event) => setTaskId(event.target.value)}
          >
            <option value="">选择任务…</option>
            {tasks.map((task) => (
              <option key={task.id} value={task.id}>
                {(task.objective || task.id).slice(0, 40)} ·{" "}
                {taskLabels[task.status] ?? task.status} ·{" "}
                {task.verified ? "已验证" : "未验证"}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="text-button"
            disabled={!taskId || linking || busy}
            onClick={() => void link(client)}
          >
            {linking ? "正在关联…" : "确认关联"}
          </button>
          <p className="field-help">
            关联只是把任务挂到这段工作下，任务自己的执行、核对与写回流程不变。
          </p>
        </>
      )}
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
