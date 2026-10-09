import type { LingApi } from "../../api/client";
import type { IdentityState, Proposal } from "../../api/types";
import { kindLabel, kindOrder } from "../../shared/identityFormatting";
import { ProposalCard } from "./ProposalCard";

function bucketOf(proposal: Proposal) {
  const kind = proposal.kind ?? "";
  return kindOrder.includes(kind) ? kind : "custom";
}

export function PendingReview({
  api,
  proposals,
  state,
  busy,
  decide,
}: {
  api: LingApi;
  proposals: Proposal[];
  state: IdentityState;
  busy: boolean;
  decide: (proposal: Proposal, decision: "approve" | "reject") => void;
}) {
  return (
    <section className="panel section-panel" aria-labelledby="pending-title">
      <div className="section-heading">
        <h2 id="pending-title">待审批</h2>
        <span className="metadata">点名字展开 · 你只决定通过 / 不通过</span>
      </div>
      <p className="muted small">
        {proposals.length === 0
          ? "灵自己提出来的变化会出现在这里。"
          : "灵自己提出来、等你决定的 " + proposals.length + " 条变化在这里。"}
        你不需要在这里写内容，只判断要不要采纳。
      </p>

      <div className="bucket-list">
        {kindOrder.map((kind) => {
          const items = proposals.filter((item) => bucketOf(item) === kind);
          return (
            <details key={kind} className="bucket">
              <summary>
                <span className="bucket-name">{kindLabel(kind)}</span>
                <span
                  className={"bucket-count" + (items.length ? " warm" : "")}
                >
                  {items.length ? items.length + " 条待你决定" : "0 条"}
                </span>
              </summary>
              <div className="bucket-body">
                {items.length === 0 ? (
                  <p className="muted small">这一部分没有待你决定的变化。</p>
                ) : (
                  <div className="stack">
                    {items.map((proposal) => (
                      <ProposalCard
                        key={proposal.id}
                        api={api}
                        proposal={proposal}
                        state={state}
                        busy={busy}
                        decide={decide}
                      />
                    ))}
                  </div>
                )}
              </div>
            </details>
          );
        })}
      </div>
    </section>
  );
}
