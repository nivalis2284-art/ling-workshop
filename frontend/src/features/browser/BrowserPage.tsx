import { useCallback } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { PageHeader } from "../../shared/ui";
import { ResourceNotice } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { browserApi } from "./api";
import { BrowserGrants } from "./BrowserGrants";
import { BrowserLogin } from "./BrowserLogin";
import { BrowserApprovals } from "./BrowserApprovals";

export function BrowserPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const service = browserApi(api, agent.id);
      const [origins, state, approvals] = await Promise.all([
        service.origins(signal),
        service.state(signal),
        service.approvals(signal),
      ]);
      return { origins, state, approvals };
    },
    [api, agent.id],
  );
  const resource = useManagementResource(load);
  return (
    <div className="management-page">
      <PageHeader
        eyebrow={agent.name + "的空间"}
        title="浏览器"
        description="管理网站授权，由你完成登录，并逐次核对需要批准的浏览器操作。"
        action={
          <button
            className="button secondary"
            onClick={() => void resource.refresh()}
          >
            刷新浏览器状态
          </button>
        }
      />
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {resource.data && (
        <>
          <BrowserGrants
            key={agent.id}
            api={api}
            value={resource.data.origins}
            refresh={resource.refresh}
          />
          <BrowserLogin
            api={api}
            state={resource.data.state}
            refresh={resource.refresh}
          />
          <BrowserApprovals
            api={api}
            agentId={agent.id}
            approvals={resource.data.approvals}
            refresh={resource.refresh}
          />
        </>
      )}
    </div>
  );
}
