import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { EmptyState, formatDate, PageHeader } from "../../shared/ui";
import { taskLabels, tasksApi } from "./api";
import { ResourceNotice } from "./ManagementUi";
import { useManagementResource } from "./useManagementResource";
import { TaskForm } from "./TaskForm";
import { TaskDetails } from "./TaskDetails";
import { BrowserPage } from "../browser/BrowserPage";

export function TasksPage({
  api,
  agent,
  openBrowser,
}: {
  api: LingApi;
  agent: Agent;
  openBrowser?: () => void;
}) {
  const load = useCallback(
    (signal: AbortSignal) => tasksApi(api).list(agent.id, signal),
    [api, agent.id],
  );
  const resource = useManagementResource(load);
  const [selected, setSelected] = useState<string | null>(null);
  const [browserReview, setBrowserReview] = useState(false);
  return (
    <div className="management-page">
      <PageHeader
        eyebrow={agent.name + "的空间"}
        title="任务"
        description="把目标交给灵，查看实际执行记录、结果与文件改动。离开页面后，任务仍由本地服务管理。"
        action={
          <button
            className="button secondary"
            onClick={() => void resource.refresh()}
          >
            刷新任务
          </button>
        }
      />
      <TaskForm
        key={agent.id}
        api={api}
        agentId={agent.id}
        onCreated={async (id) => {
          setSelected(id);
          await resource.refresh();
        }}
      />
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {resource.data && (
        <section className="stack" aria-label="任务列表">
          <h2>已有任务</h2>
          {resource.data.length === 0 ? (
            <EmptyState title="还没有任务">
              填写一个明确目标，即可创建任务。
            </EmptyState>
          ) : (
            <ul className="management-list">
              {resource.data.map((task) => (
                <li key={task.id} className="panel management-card">
                  <div className="section-heading">
                    <h3>{task.objective}</h3>
                    <span className="pill">
                      {taskLabels[task.status] ?? task.status}
                    </span>
                  </div>
                  <p className="metadata">
                    {formatDate(task.updated_at)} ·{" "}
                    {task.verified ? "结果已验证" : "结果尚未验证"}
                  </p>
                  {task.final && (
                    <p className="management-copy">{task.final}</p>
                  )}
                  <button
                    className="button secondary"
                    aria-pressed={selected === task.id}
                    onClick={() => setSelected(task.id)}
                  >
                    查看任务
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      {selected && (
        <TaskDetails
          key={agent.id + selected}
          api={api}
          id={selected}
          refreshList={resource.refresh}
          close={() => setSelected(null)}
          openBrowser={openBrowser ?? (() => setBrowserReview(true))}
        />
      )}
      {browserReview && (
        <section>
          <button
            className="text-button"
            onClick={() => setBrowserReview(false)}
          >
            关闭浏览器审阅
          </button>
          <BrowserPage api={api} agent={agent} />
        </section>
      )}
    </div>
  );
}
