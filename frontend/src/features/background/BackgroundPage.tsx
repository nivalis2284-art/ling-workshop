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
import { RecordData, ResourceNotice } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { useManagementAction } from "../tasks/useManagementAction";
import { backgroundApi, backgroundLabels, jobLabels } from "./api";
import type { BackgroundJob, BackgroundResult } from "./api";

export function BackgroundPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const service = backgroundApi(api);
      const [status, jobs] = await Promise.all([
        service.status(signal),
        service.jobs(signal),
      ]);
      return { status, jobs };
    },
    [api],
  );
  const resource = useManagementResource(load);
  const action = useManagementAction(resource.refresh);
  const [retry, setRetry] = useState<BackgroundJob | null>(null);
  const [result, setResult] = useState<BackgroundResult | null>(null);
  const status = resource.data?.status;
  const jobs =
    resource.data?.jobs.filter((job) => job.agent_id === agent.id) ?? [];
  const last = result ?? status?.last_result;
  return (
    <div className="management-page">
      <PageHeader
        eyebrow="本地空间"
        title="后台工作"
        description="管理本地服务的后台开关，查看实际工作记录。后台开关和运行一次会作用于所有灵；下方工作列表显示当前灵最近的记录。"
        action={
          <button
            className="button secondary"
            onClick={() => void resource.refresh()}
          >
            刷新后台状态
          </button>
        }
      />
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {action.error && <ErrorNotice message={action.error} />}
      {status && (
        <section className="panel section-panel stack">
          <h2>{status.enabled ? "后台已启用" : "后台已暂停"}</h2>
          <p>
            {status.working ? "正在处理后台工作" : "当前没有后台工作在运行"} ·
            等待处理 {status.queued} 项
          </p>
          <p className="muted small">
            服务每 {status.interval_seconds}{" "}
            秒检查一次。暂停后台会停止当前后台处理；离开这个页面只停止读取状态。
          </p>
          <div className="button-row">
            <button
              className="button secondary"
              disabled={action.busy}
              onClick={() =>
                void action.run(async () => {
                  await backgroundApi(api).configure(!status.enabled);
                  setResult(null);
                })
              }
            >
              {status.enabled ? "暂停后台" : "启用后台"}
            </button>
            <button
              className="button primary"
              disabled={action.busy || !status.enabled || status.working}
              onClick={() =>
                void action.run(async () =>
                  setResult(await backgroundApi(api).runOnce()),
                )
              }
            >
              {action.busy ? "正在处理…" : "运行一次"}
            </button>
          </div>
          <p className="muted small">
            运行一次会尝试处理一项到期活动或后台工作，可能使用已配置的模型。
          </p>
          {last && (
            <div>
              <h3>最近一次运行结果</h3>
              <p role="status">
                {backgroundLabels[last.status] ??
                  jobLabels[last.status] ??
                  "服务状态：" + last.status}
              </p>
              {last.error_code && (
                <p className="inline-warning">原因：{last.error_code}</p>
              )}
              <RecordData value={last} label="查看运行详情" />
            </div>
          )}
        </section>
      )}
      {resource.data && (
        <section className="stack" aria-label="后台工作列表">
          <h2>{agent.name}的近期工作</h2>
          {jobs.length === 0 && (
            <EmptyState title="还没有后台工作记录">
              服务创建后台工作后，这里会显示实际状态。
            </EmptyState>
          )}
          {jobs.map((job) => (
            <article className="panel management-card" key={job.id}>
              <div className="section-heading">
                <h3>
                  {job.kind === "consolidate" ? "整理经历与记忆" : job.kind}
                </h3>
                <span className="pill">
                  {jobLabels[job.status] ?? job.status}
                </span>
              </div>
              <p className="metadata">
                {formatDate(job.created_at)} · 已尝试 {job.attempts ?? 0} 次
              </p>
              {job.outcome && (
                <p>实际结果：{backgroundLabels[job.outcome] ?? job.outcome}</p>
              )}
              {job.error_code && (
                <p className="inline-warning">失败原因：{job.error_code}</p>
              )}
              {job.status === "failed" && job.kind === "consolidate" && (
                <button
                  className="button secondary"
                  disabled={action.busy}
                  onClick={() => setRetry(job)}
                >
                  重试失败工作
                </button>
              )}
            </article>
          ))}
          <p className="metadata">
            显示服务最近 100 条后台记录中属于当前灵的工作。
          </p>
        </section>
      )}
      {retry && (
        <ConfirmDialog
          title="重试失败的后台工作"
          confirmLabel="确认重试"
          busy={action.busy}
          onCancel={() => setRetry(null)}
          onConfirm={() =>
            void action.run(async () => {
              await backgroundApi(api).retry(retry.id);
              setRetry(null);
            })
          }
        >
          <p>这会把失败工作重新放入队列，并授权再次调用模型，可能产生费用。</p>
          <p>上次失败原因：{retry.error_code ?? "服务未提供"}</p>
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </div>
  );
}
