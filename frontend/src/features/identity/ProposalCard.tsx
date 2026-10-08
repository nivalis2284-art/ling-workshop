import { Check, X } from "lucide-react";
import type { LingApi } from "../../api/client";
import type { IdentityState, Proposal } from "../../api/types";
import {
  identityEntryLabel,
  identityEntryText,
} from "../../shared/identityFormatting";
import { Sources } from "../sources/SourceViewer";

const kinds: Record<string, string> = {
  habit: "习惯",
  identity: "身份理解",
  relationship: "关系",
  value: "价值观",
  major_personality: "性格变化",
  major_capability: "能力变化",
};
const statuses: Record<string, string> = {
  awaiting_owner: "待审阅",
  applied_owner: "已批准",
  applied_auto: "已自动应用",
  rejected: "已拒绝",
  stale: "依据已失效",
};

export function ProposalCard({
  api,
  proposal,
  state,
  busy,
  decide,
}: {
  api: LingApi;
  proposal: Proposal;
  state: IdentityState;
  busy: boolean;
  decide: (proposal: Proposal, decision: "approve" | "reject") => void;
}) {
  const pending = proposal.status === "awaiting_owner";
  const outdated = proposal.base_revision !== state.revision;
  return (
    <article className="proposal-card">
      <div className="section-heading">
        <h3>{kinds[proposal.kind ?? ""] ?? "身份变化"}</h3>
        <span className={"pill" + (pending ? " warm" : "")}>
          {statuses[proposal.status] ?? "未知状态：" + proposal.status}
        </span>
      </div>
      <p className="metadata break-anywhere">
        {identityEntryLabel(proposal.key)} · 基于版本 {proposal.base_revision}
      </p>
      {pending && (
        <div className="comparison">
          <div>
            <span className="field-label">当前内容</span>
            <p>
              {state.entries[proposal.key] === undefined
                ? "尚未形成这项理解"
                : identityEntryText(proposal.key, state.entries[proposal.key])}
            </p>
          </div>
          <div className="proposed">
            <span className="field-label">建议变化</span>
            <p>{identityEntryText(proposal.key, proposal.text)}</p>
          </div>
        </div>
      )}
      {!pending && (
        <p className="preserve-text">
          {identityEntryText(proposal.key, proposal.text)}
        </p>
      )}
      <Sources api={api} sources={proposal.sources} />
      {pending && (
        <>
          {outdated && (
            <p className="inline-warning">
              候选基于旧版本，需要重新评估后才能批准。你仍可以拒绝这项变化。
            </p>
          )}
          <div className="proposal-actions">
            <p className="small muted">请先核对内容与来源。</p>
            <div className="button-row">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => decide(proposal, "reject")}
              >
                <X size={15} aria-hidden="true" />
                拒绝这项变化
              </button>
              <button
                className="button primary"
                disabled={busy || outdated}
                onClick={() => decide(proposal, "approve")}
              >
                <Check size={15} aria-hidden="true" />
                批准这项变化
              </button>
            </div>
          </div>
        </>
      )}
    </article>
  );
}
