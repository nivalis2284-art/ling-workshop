import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { EmptyState, ErrorNotice } from "../../shared/ui";
import { Sources } from "../sources/SourceViewer";
import { ResourceNotice } from "../tasks/ManagementUi";
import { useManagementAction } from "../tasks/useManagementAction";
import { useManagementResource } from "../tasks/useManagementResource";
import { developmentApi, goalLabels } from "./api";
import type { Goal, GoalAction } from "./api";
import { GoalForm } from "./GoalForm";

export function GoalsPanel({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    (signal: AbortSignal) => developmentApi(api).list(agent.id, signal),
    [api, agent.id],
  );
  const resource = useManagementResource(load);
  const action = useManagementAction(resource.refresh);
  const [confirm, setConfirm] = useState<{
    goal: Goal;
    action: "complete" | "abandon";
  } | null>(null);
  function decide(goal: Goal, decision: GoalAction) {
    if (decision === "complete" || decision === "abandon")
      setConfirm({ goal, action: decision });
    else void action.run(() => developmentApi(api).decide(goal, decision));
  }
  return (
    <section className="panel section-panel stack" aria-label="长期目标">
      <div className="section-heading">
        <h2>长期目标</h2>
        <button className="text-button" onClick={() => void resource.refresh()}>
          刷新目标
        </button>
      </div>
      <p className="muted small">
        目标记录长期方向；任务执行具体工作，活动负责定时关注。进展只展示服务已经记录的内容。
      </p>
      <GoalForm
        key={agent.id}
        api={api}
        agentId={agent.id}
        refresh={resource.refresh}
      />
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {action.error && <ErrorNotice message={action.error} />}
      {resource.data?.length === 0 && (
        <EmptyState title="还没有长期目标">
          提出一个方向，再核对并启用。
        </EmptyState>
      )}
      {resource.data?.map((goal) => (
        <article key={goal.id} className="panel management-card">
          <div className="section-heading">
            <h3>{goal.title}</h3>
            <span className="pill">
              {goalLabels[goal.status] ?? goal.status}
            </span>
          </div>
          <p className="metadata">版本 {goal.revision}</p>
          <p className="field-label">已记录的进展</p>
          <p className="management-copy">{goal.progress || "尚未记录进展。"}</p>
          <Sources api={api} sources={goal.sources} />
          {goal.status === "needs_review" && (
            <p className="inline-warning">来源发生变化，请重新核对目标依据。</p>
          )}
          <div className="button-row">
            {["proposed", "paused"].includes(goal.status) && (
              <button
                className="button primary"
                disabled={action.busy}
                onClick={() => decide(goal, "activate")}
              >
                {goal.status === "proposed" ? "启用目标" : "恢复目标"}
              </button>
            )}
            {goal.status === "active" && (
              <>
                <button
                  className="button secondary"
                  disabled={action.busy}
                  onClick={() => decide(goal, "pause")}
                >
                  暂停目标
                </button>
                <button
                  className="button secondary"
                  disabled={action.busy}
                  onClick={() => decide(goal, "complete")}
                >
                  标记目标完成
                </button>
              </>
            )}
            {["proposed", "active", "paused", "needs_review"].includes(
              goal.status,
            ) && (
              <button
                className="text-button"
                disabled={action.busy}
                onClick={() => decide(goal, "abandon")}
              >
                放弃目标
              </button>
            )}
          </div>
        </article>
      ))}
      {confirm && (
        <ConfirmDialog
          title={
            confirm.action === "complete"
              ? "标记这个目标为完成"
              : "放弃这个目标"
          }
          confirmLabel={
            confirm.action === "complete" ? "确认完成目标" : "确认放弃目标"
          }
          busy={action.busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() =>
            void action.run(async () => {
              await developmentApi(api).decide(confirm.goal, confirm.action);
              setConfirm(null);
            })
          }
        >
          <p>{confirm.goal.title}</p>
          <p>这会结束这个目标，已有进展与来源会保留。</p>
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
