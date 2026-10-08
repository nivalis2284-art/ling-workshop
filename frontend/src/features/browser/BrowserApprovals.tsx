import { useState } from "react";
import type { LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { EmptyState, ErrorNotice, formatDate } from "../../shared/ui";
import { RecordData } from "../tasks/ManagementUi";
import { useManagementAction } from "../tasks/useManagementAction";
import { browserApi, browserResult } from "./api";
import type { BrowserAction, BrowserApproval } from "./api";
import { ApprovalIntent } from "./ApprovalIntent";

export function BrowserApprovals({
  api,
  agentId,
  approvals,
  refresh,
}: {
  api: LingApi;
  agentId: string;
  approvals: BrowserApproval[];
  refresh: () => Promise<void>;
}) {
  const action = useManagementAction(refresh);
  const [confirm, setConfirm] = useState<BrowserApproval | null>(null);
  const [result, setResult] = useState<{
    value: BrowserAction;
    decision: "approve" | "reject";
  } | null>(null);
  async function decide(
    approval: BrowserApproval,
    decision: "approve" | "reject",
  ) {
    await action.run(async () => {
      const value = await browserApi(api, agentId).decide(approval, decision);
      setResult({ value, decision });
      setConfirm(null);
    });
  }
  return (
    <section className="stack" aria-label="浏览器待审批操作">
      <h2>待你批准的操作</h2>
      <p className="muted small">
        先核对页面、目标和表单内容。批准仅授权当前展示的这一次操作。
      </p>
      {action.error && <ErrorNotice message={action.error} />}
      {result && (
        <div className="panel management-card">
          <p role="status">{browserResult(result.value, result.decision)}</p>
          <RecordData value={result.value} label="查看操作结果" />
        </div>
      )}
      {approvals.length === 0 && (
        <EmptyState title="没有待审批操作">
          服务准备好需要批准的操作后，会显示具体内容。
        </EmptyState>
      )}
      {approvals.map((approval) => (
        <article className="panel management-card" key={approval.id}>
          <p className="metadata">{formatDate(approval.created_at)}</p>
          <ApprovalIntent approval={approval} />
          <div className="button-row">
            <button
              className="button primary"
              disabled={action.busy}
              onClick={() => setConfirm(approval)}
            >
              批准这次操作
            </button>
            <button
              className="button secondary"
              disabled={action.busy}
              onClick={() => void decide(approval, "reject")}
            >
              拒绝这次操作
            </button>
          </div>
        </article>
      ))}
      {confirm && (
        <ConfirmDialog
          title="批准这次浏览器操作"
          confirmLabel="确认批准"
          busy={action.busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void decide(confirm, "approve")}
        >
          <p>确认后，浏览器将执行下面这一次操作。</p>
          <ApprovalIntent approval={confirm} />
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
