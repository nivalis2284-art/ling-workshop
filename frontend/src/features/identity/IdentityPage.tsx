import { useCallback, useState } from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { ApiError, errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent, Proposal, Revision } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { EmptyState, ErrorNotice, Loading, PageHeader } from "../../shared/ui";
import { useResource } from "../../shared/useResource";
import {
  identityEntryLabel,
  identityEntryText,
} from "../../shared/identityFormatting";
import { ProposalCard } from "./ProposalCard";
import { RevisionHistory } from "./RevisionHistory";
import { IdentityEditor } from "./IdentityEditor";

export function IdentityPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    (signal: AbortSignal) => api.identity(agent.id, signal),
    [api, agent.id],
  );
  const resource = useResource(load);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [target, setTarget] = useState<Revision | null>(null);

  async function mutate(action: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    let completed = false;
    try {
      await action();
      completed = true;
    } catch (error) {
      setError(
        error instanceof ApiError && error.status === 409
          ? "版本或候选状态已变化，请核对最新资料后再决定。"
          : errorMessage(error),
      );
    } finally {
      setTarget(null);
      // No optimistic revision increments: the backend is authoritative, including on conflicts.
      await resource.refresh();
      if (completed) setMessage(success);
      setBusy(false);
    }
  }
  function decide(proposal: Proposal, decision: "approve" | "reject") {
    if (resource.status !== "ready") return;
    void mutate(
      () => api.decide(proposal.id, decision, resource.data.state.revision),
      decision === "approve" ? "变化已生效。" : "已拒绝这项变化。",
    );
  }

  const pending =
    resource.data?.proposals.filter(
      (item) => item.status === "awaiting_owner",
    ) ?? [];
  const handled =
    resource.data?.proposals.filter(
      (item) => item.status !== "awaiting_owner",
    ) ?? [];
  return (
    <div className="page">
      <PageHeader
        eyebrow="理解自己，也由你把关"
        title="身份与变化"
        description={
          "查看" + agent.name + "当前的理解，并审阅每一项有来源的变化。"
        }
        action={
          <button
            className="button secondary"
            disabled={busy || resource.status === "loading"}
            onClick={() => void resource.refresh()}
          >
            <RefreshCw size={15} aria-hidden="true" />
            刷新资料
          </button>
        }
      />
      {error && <ErrorNotice message={error} />}
      {message && resource.status === "ready" && (
        <div className="notice success" role="status">
          <ShieldCheck size={19} aria-hidden="true" />
          <p>{message}</p>
        </div>
      )}
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          <section className="panel section-panel" aria-label="当前身份">
            <div className="section-heading">
              <h2>当前身份</h2>
              <span className="pill">
                当前版本 · {resource.data.state.revision}
              </span>
            </div>
            {Object.keys(resource.data.state.entries).length === 0 ? (
              <EmptyState title="还在认识自己的起点">
                当前没有已生效的身份内容。经过审阅的变化会在这里积累。
              </EmptyState>
            ) : (
              <dl className="entry-list">
                {Object.entries(resource.data.state.entries).map(
                  ([key, text]) => (
                    <div key={key}>
                      <dt>{identityEntryLabel(key)}</dt>
                      <dd>{identityEntryText(key, text)}</dd>
                    </div>
                  ),
                )}
              </dl>
            )}
          </section>
          <section aria-labelledby="pending-title">
            <div className="section-heading outside">
              <h2 id="pending-title">
                等待你的审阅 <span className="count">{pending.length}</span>
              </h2>
              <span className="metadata">先看依据，再做决定</span>
            </div>
            {pending.length === 0 ? (
              <div className="panel">
                <EmptyState title="暂时没有待审阅的变化">
                  新的候选会出现在这里，等待你的判断。
                </EmptyState>
              </div>
            ) : (
              <div className="stack">
                {pending.map((proposal) => (
                  <ProposalCard
                    key={proposal.id}
                    api={api}
                    proposal={proposal}
                    state={resource.data.state}
                    busy={busy}
                    decide={decide}
                  />
                ))}
              </div>
            )}
          </section>
          {handled.length > 0 && (
            <details className="panel handled">
              <summary>其他候选与审阅结果 · {handled.length} 项</summary>
              <div className="stack">
                {handled.map((proposal) => (
                  <ProposalCard
                    key={proposal.id}
                    api={api}
                    proposal={proposal}
                    state={resource.data.state}
                    busy={busy}
                    decide={decide}
                  />
                ))}
              </div>
            </details>
          )}
          <RevisionHistory
            revisions={resource.data.revisions}
            current={resource.data.state.revision}
            busy={busy}
            restore={setTarget}
          />
          <IdentityEditor api={api} agentId={agent.id} revision={resource.data.state.revision} refresh={resource.refresh}/>
          {target && (
            <ConfirmDialog
              title={"恢复到版本 " + target.revision}
              confirmLabel="确认恢复"
              busy={busy}
              onCancel={() => setTarget(null)}
              onConfirm={() =>
                void mutate(
                  () =>
                    api.rollback(
                      agent.id,
                      target.revision,
                      resource.data.state.revision,
                    ),
                  "身份已恢复，新版本已更新。",
                )
              }
            >
              <p>
                将把版本 {target.revision}{" "}
                的身份内容恢复为一个新版本，当前版本为{" "}
                {resource.data.state.revision}。已有历史不会删除。
              </p>
              {Object.keys(target.entries).length === 0 ? (
                <p>目标版本没有身份内容。</p>
              ) : (
                <ul>
                  {Object.entries(target.entries).map(([key, text]) => (
                    <li key={key}>
                      {identityEntryLabel(key)}：{identityEntryText(key, text)}
                    </li>
                  ))}
                </ul>
              )}
              <p>如果原始依据已失效，本次恢复会被拒绝。</p>
            </ConfirmDialog>
          )}
        </>
      )}
    </div>
  );
}
