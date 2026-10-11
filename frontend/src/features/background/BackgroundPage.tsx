import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { ErrorNotice, PageHeader } from "../../shared/ui";
import { RecordData, ResourceNotice } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { useManagementAction } from "../tasks/useManagementAction";
import { backgroundApi, backgroundLabels } from "./api";
import type { BackgroundResult } from "./api";

export function BackgroundPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    (signal: AbortSignal) => backgroundApi(api).status(signal),
    [api],
  );
  const resource = useManagementResource(load);
  const action = useManagementAction(resource.refresh);
  const [result, setResult] = useState<BackgroundResult | null>(null);
  const status = resource.data;
  const last = result ?? status?.last_result;
  return (
    <div className="management-page">
      <PageHeader
        eyebrow="本地空间"
        title="后台工作"
        description={
          agent.name +
          " 的后台协调器只负责每日自动备份：空闲时按计划检查一次，不会处理其它队列。"
        }
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
            {status.working ? "正在检查自动备份" : "当前没有后台工作在运行"}
          </p>
          <p className="muted small">
            服务每 {status.interval_seconds}{" "}
            秒检查一次。暂停后台会停止自动备份；离开这个页面只停止读取状态。
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
              {action.busy ? "正在检查…" : "立即检查一次"}
            </button>
          </div>
          <p className="muted small">
            立即检查会尝试执行一次自动备份，不会调用模型。
          </p>
          {last && (
            <div>
              <h3>最近一次运行结果</h3>
              <p role="status">
                {backgroundLabels[last.status] ?? "服务状态：" + last.status}
              </p>
              {last.error_code && (
                <p className="inline-warning">原因：{last.error_code}</p>
              )}
              <RecordData value={last} label="查看运行详情" />
            </div>
          )}
        </section>
      )}
    </div>
  );
}
