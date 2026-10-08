import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice } from "../../shared/ui";
import { tasksApi, taskLabels } from "./api";
import { ResourceNotice } from "./ManagementUi";
import { useManagementResource } from "./useManagementResource";
import { useManagementAction } from "./useManagementAction";
import { TaskEvents } from "./TaskEvents";
import { TaskChanges } from "./TaskChanges";
import { TaskDependencies } from "./TaskDependencies";
import { TaskSteering } from "./TaskSteering";

export function TaskDetails({
  api,
  id,
  refreshList,
  close,
  openBrowser,
}: {
  api: LingApi;
  id: string;
  refreshList: () => Promise<void>;
  close: () => void;
  openBrowser: () => void;
}) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const service = tasksApi(api);
      const [task, events] = await Promise.all([
        service.read(id, signal),
        service.events(id, signal),
      ]);
      return { task, events };
    },
    [api, id],
  );
  const resource = useManagementResource(load);
  const refresh = async () => {
    await Promise.all([resource.refresh(), refreshList()]);
  };
  const action = useManagementAction(refresh);
  const [confirm, setConfirm] = useState(false);
  const [steps, setSteps] = useState(0);
  const task = resource.data?.task;
  const running = task?.status === "queued" || task?.status === "running";
  const resumable =
    task &&
    ["paused", "blocked", "interrupted", "waiting_owner"].includes(task.status);
  return (
    <section className="panel section-panel stack" aria-label="任务详情">
      <div className="section-heading">
        <h2>任务详情</h2>
        <button className="text-button" onClick={close}>
          关闭详情
        </button>
      </div>
      <ResourceNotice {...resource} hasData={Boolean(task)} />
      {action.error && <ErrorNotice message={action.error} />}
      {task && (
        <>
          <h3 className="management-copy">{task.objective}</h3>
          <p className="metadata">
            {taskLabels[task.status] ?? task.status} · 已执行 {task.step_count}{" "}
            次处理 · 当前上限 {task.step_limit} 次
          </p>
          <p>{task.verified ? "已验证结果" : "结果尚未验证"}</p>
          <p className="metadata">
            执行环境：
            {task.environment === "node" ? "Node.js / TypeScript" : "Python"}
          </p>
          {task.error_code && (
            <p className="inline-warning">任务需要检查：{task.error_code}</p>
          )}
          <div className="button-row">
            {task.status === "waiting_owner" && (
              <button className="button primary" onClick={openBrowser}>
                前往浏览器确认
              </button>
            )}
            {running && (
              <button
                className="button secondary"
                disabled={action.busy}
                onClick={() =>
                  void action.run(() => tasksApi(api).control(id, "pause"))
                }
              >
                暂停任务
              </button>
            )}
            {resumable && (
              <>
                <label className="metadata">
                  额外处理次数
                  <input
                    type="number"
                    min={0}
                    max={1000}
                    step={1}
                    value={steps}
                    onChange={(event) => setSteps(Number(event.target.value))}
                  />
                </label>
                <button
                  className="button primary"
                  disabled={
                    action.busy ||
                    !Number.isInteger(steps) ||
                    steps < 0 ||
                    steps > 1000 ||
                    (task.step_count >= task.step_limit && steps === 0)
                  }
                  onClick={() =>
                    void action.run(() =>
                      tasksApi(api).control(id, "resume", steps),
                    )
                  }
                >
                  恢复任务
                </button>
              </>
            )}
            {(running || resumable) && (
              <button
                className="button secondary"
                disabled={action.busy}
                onClick={() => setConfirm(true)}
              >
                取消任务
              </button>
            )}
          </div>
          {task.status === "waiting_owner" && (
            <p className="inline-warning">
              需确认：请到浏览器页面核对并批准或拒绝本次操作，再点击「恢复任务」。审批不会自动恢复任务。
            </p>
          )}
          {resumable && (
            <p className="muted small">
              增加处理次数可能产生新的模型调用。达到当前上限时，需要填写大于零的额外次数。
            </p>
          )}
          {(running||resumable)&&<TaskSteering api={api} id={task.id} onUpdated={refresh}/>}
          <section>
            <h3>最终答复</h3>
            <p className="management-copy">
              {task.final ?? "服务尚未返回最终答复。"}
            </p>
          </section>
          {task.workspace && (
            <TaskChanges api={api} task={task} onApplied={refresh} />
          )}
          {task.workspace && ["paused","blocked"].includes(task.status) && <TaskDependencies key={JSON.stringify(task.dependency_candidate??task.id)} api={api} taskId={task.id} status={task.status} onUpdated={refresh} candidate={task.dependency_candidate}/>}
          <TaskEvents events={resource.data!.events} />
        </>
      )}
      {confirm && (
        <ConfirmDialog
          title="取消这项任务"
          confirmLabel="确认取消任务"
          busy={action.busy}
          onCancel={() => setConfirm(false)}
          onConfirm={() =>
            void action.run(async () => {
              await tasksApi(api).control(id, "cancel");
              setConfirm(false);
            })
          }
        >
          <p>取消后无法恢复。已经产生的执行记录和成果会保留。</p>
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
