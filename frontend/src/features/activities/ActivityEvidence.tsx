import { useCallback } from "react";
import type { LingApi } from "../../api/client";
import { EmptyState, formatDate } from "../../shared/ui";
import { Sources } from "../sources/SourceViewer";
import { ResourceNotice, RecordData } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { activitiesApi } from "./api";

export function ActivityEvidence({
  api,
  id,
  close,
}: {
  api: LingApi;
  id: string;
  close: () => void;
}) {
  const load = useCallback(
    (signal: AbortSignal) => activitiesApi(api).evidence(id, signal),
    [api, id],
  );
  const resource = useManagementResource(load);
  return (
    <section className="panel section-panel stack" aria-label="活动检查记录">
      <div className="section-heading">
        <h2>实际检查记录</h2>
        <button className="text-button" onClick={close}>
          关闭记录
        </button>
      </div>
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {resource.data?.length === 0 && (
        <EmptyState title="还没有检查记录">
          服务尚未记录新的提醒或关注内容。
        </EmptyState>
      )}
      {resource.data?.map((item) => (
        <article key={item.id} className="panel management-card">
          <p className="metadata">{formatDate(item.observed_at)}</p>
          {typeof item.payload.summary === "string" && (
            <p className="management-copy">{item.payload.summary}</p>
          )}
          {typeof item.payload.excerpt === "string" && (
            <p className="management-copy">{item.payload.excerpt}</p>
          )}
          <RecordData value={item.payload} label="查看检查详情" />
          <Sources api={api} sources={[item.source]} />
        </article>
      ))}
    </section>
  );
}
