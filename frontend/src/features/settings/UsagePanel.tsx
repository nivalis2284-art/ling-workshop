import { useCallback } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";
import "./management.css";

export function UsagePanel({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    (signal: AbortSignal) => api.usage(agent.id, signal),
    [api, agent.id],
  );
  const resource = useResource(load);
  return (
    <section
      className="panel section-panel usage-panel"
      aria-labelledby="usage-heading"
    >
      <div className="section-heading">
        <h2 id="usage-heading">用量</h2>
        <button
          className="text-button"
          disabled={resource.status === "loading"}
          onClick={() => void resource.refresh()}
        >
          刷新用量
        </button>
      </div>
      <p className="muted">{agent.name} · 已记录的模型调用</p>
      {resource.status === "loading" && <Loading label="正在读取用量…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          <dl className="usage-values">
            {(
              [
                ["调用次数", resource.data.calls],
                ["已报告用量的调用", resource.data.reported_calls],
                ["输入 tokens", resource.data.input_tokens],
                ["输出 tokens", resource.data.output_tokens],
                ["服务商总 tokens", resource.data.provider_total_tokens],
                ["费用估算（USD）", resource.data.cost_usd ?? "未知"],
              ] as const
            ).map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd aria-label={label}>
                  {value === null
                    ? "未提供"
                    : typeof value === "number"
                      ? value.toLocaleString("zh-CN")
                      : value}
                </dd>
              </div>
            ))}
          </dl>
          <p className="muted">{resource.data.note}</p>
          <p className="field-help">
            缺失的用量不按零计算；服务商总量直接采用报告值。费用按调用时配置的渠道价格估算，以实际账单为准。
          </p>
          {resource.data.cost_status === "partial" && (
            <p className="field-help">
              仅 {resource.data.priced_calls} 次调用可估算，已知部分为 USD{" "}
              {resource.data.estimated_cost_usd}；完整总额仍未知。
            </p>
          )}
        </>
      )}
    </section>
  );
}
