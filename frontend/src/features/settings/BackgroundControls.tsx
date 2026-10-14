import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { ErrorNotice } from "../../shared/ui";
import { RecordData, ResourceNotice } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { useManagementAction } from "../tasks/useManagementAction";
import { backgroundApi, backgroundLabels } from "../background/api";
import type { BackgroundResult } from "../background/api";

/** 「后台工作」并入设置（2026-10-15 所有者定案）：启用/暂停、立即检查、最近结果——
 * 原独立页的同一组操作，用同一组路由。 */
export function BackgroundControls({
  api,
  agent,
}: {
  api: LingApi;
  agent: Agent;
}) {
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
    <section className="panel section-panel stack" aria-label="后台运行">
      <div className="section-heading">
        <h3>后台运行</h3>
        <button
          className="button small-button secondary"
          onClick={() => void resource.refresh()}
        >
          刷新
        </button>
      </div>
      <p className="muted">
        {agent.name} 的后台协调器只负责每日自动备份：空闲时按计划检查一次，不会处理其它队列。
      </p>
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {action.error && <ErrorNotice message={action.error} />}
      {status && (
        <>
          <p>
            {status.enabled ? "后台已启用" : "后台已暂停"} ·{" "}
            {status.working ? "正在检查自动备份" : "当前没有后台工作在运行"}
          </p>
          <p className="metadata">
            服务每 {status.interval_seconds} 秒检查一次。暂停后台会停止自动备份；离开这一页只停止读取状态。
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
          <p className="metadata">立即检查会尝试执行一次自动备份，不会调用模型。</p>
          {last && (
            <div>
              <h4>最近一次运行结果</h4>
              <p role="status">
                {backgroundLabels[last.status] ?? "服务状态：" + last.status}
              </p>
              {last.error_code && (
                <p className="inline-warning">原因：{last.error_code}</p>
              )}
              <RecordData value={last} label="查看运行详情" />
            </div>
          )}
        </>
      )}
    </section>
  );
}
