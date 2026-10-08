import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import {
  EmptyState,
  ErrorNotice,
  formatDate,
  PageHeader,
} from "../../shared/ui";
import { ResourceNotice } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { useManagementAction } from "../tasks/useManagementAction";
import {
  activitiesApi,
  activityLabels,
  kindLabels,
  outcomeLabels,
} from "./api";
import type { Activity } from "./api";
import { ActivityForm } from "./ActivityForm";
import { WebOriginsForm } from "./WebOriginsForm";
import { ActivityEvidence } from "./ActivityEvidence";
import { GoalsPanel } from "../development/GoalsPanel";

export function ActivitiesPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const service = activitiesApi(api);
      const [activities, notifications, origins] = await Promise.all([
        service.list(agent.id, signal),
        service.notifications(agent.id, signal),
        service.origins(agent.id, signal),
      ]);
      return { activities, notifications, origins };
    },
    [api, agent.id],
  );
  const resource = useManagementResource(load);
  const action = useManagementAction(resource.refresh);
  const [cancel, setCancel] = useState<Activity | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <div className="management-page">
      <PageHeader
        eyebrow={agent.name + "的空间"}
        title="活动"
        description="安排提醒、关注文件或网页变化，并定期复查主题。通知只展示服务实际记录的消息。"
        action={
          <button
            className="button secondary"
            onClick={() => void resource.refresh()}
          >
            刷新活动
          </button>
        }
      />
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {resource.data && (
        <>
          <ActivityForm
            key={agent.id}
            api={api}
            agentId={agent.id}
            origins={resource.data.origins}
            refresh={resource.refresh}
          />
          <WebOriginsForm
            api={api}
            value={resource.data.origins}
            refresh={resource.refresh}
          />
          {action.error && <ErrorNotice message={action.error} />}
          <section className="stack" aria-label="活动列表">
            <h2>已安排的活动</h2>
            {resource.data.activities.length === 0 && (
              <EmptyState title="还没有活动">
                选择一种活动，填写标题与时间。
              </EmptyState>
            )}
            {resource.data.activities.map((item) => (
              <article key={item.id} className="panel management-card">
                <div className="section-heading">
                  <h3>{item.title}</h3>
                  <span className="pill">
                    {activityLabels[item.status] ?? item.status}
                  </span>
                </div>
                <p className="metadata">
                  {kindLabels[item.kind]} · 版本 {item.revision} · 下次时间{" "}
                  {formatDate(item.next_due_at)}
                </p>
                {item.target && (
                  <p className="management-copy">{item.target}</p>
                )}
                <p className="metadata">
                  {item.interval_seconds === null
                    ? "执行一次"
                    : "每 " + item.interval_seconds / 60 + " 分钟执行"}
                  {item.busy ? " · 正在检查" : ""}
                </p>
                {item.last_outcome && (
                  <p>
                    最近检查：
                    {outcomeLabels[item.last_outcome] ?? item.last_outcome} ·{" "}
                    {formatDate(item.last_checked_at ?? undefined)}
                  </p>
                )}
                {item.last_error && (
                  <p className="inline-warning">
                    检查未完成：{item.last_error}
                  </p>
                )}
                <div className="button-row">
                  {item.status === "active" && (
                    <button
                      className="button secondary"
                      disabled={action.busy}
                      onClick={() =>
                        void action.run(() =>
                          activitiesApi(api).control(item, "pause"),
                        )
                      }
                    >
                      暂停活动
                    </button>
                  )}
                  {item.status === "paused" && (
                    <button
                      className="button primary"
                      disabled={action.busy}
                      onClick={() =>
                        void action.run(() =>
                          activitiesApi(api).control(item, "resume"),
                        )
                      }
                    >
                      恢复活动
                    </button>
                  )}
                  {["active", "paused"].includes(item.status) && (
                    <button
                      className="button secondary"
                      disabled={action.busy}
                      onClick={() => setCancel(item)}
                    >
                      取消活动
                    </button>
                  )}
                  <button
                    className="button secondary"
                    onClick={() => setSelected(item.id)}
                  >
                    查看检查记录
                  </button>
                </div>
              </article>
            ))}
          </section>
          <section className="panel section-panel stack" aria-label="活动通知">
            <h2>活动通知</h2>
            {resource.data.notifications.length === 0 && (
              <EmptyState title="还没有活动通知">
                出现实际提醒或变化时，通知会显示在这里。
              </EmptyState>
            )}
            {[...resource.data.notifications].reverse().map((notice) => (
              <article key={notice.id}>
                <p className="management-copy">{notice.message}</p>
                <p className="metadata">{formatDate(notice.created_at)}</p>
                <button
                  className="text-button"
                  onClick={() => setSelected(notice.activity_id)}
                >
                  查看通知依据
                </button>
              </article>
            ))}
          </section>
        </>
      )}
      {selected && (
        <ActivityEvidence
          key={selected}
          api={api}
          id={selected}
          close={() => setSelected(null)}
        />
      )}
      <GoalsPanel api={api} agent={agent} />
      {cancel && (
        <ConfirmDialog
          title="取消这项活动"
          confirmLabel="确认取消活动"
          busy={action.busy}
          onCancel={() => setCancel(null)}
          onConfirm={() =>
            void action.run(async () => {
              await activitiesApi(api).control(cancel, "cancel");
              setCancel(null);
            })
          }
        >
          <p>取消「{cancel.title}」后不会再执行。已有通知和检查记录会保留。</p>
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </div>
  );
}
