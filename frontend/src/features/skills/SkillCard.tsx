import type { LingApi } from "../../api/client";
import type { Skill, SkillAction } from "./api";
import { skillLabels } from "./api";
import { Sources } from "../sources/SourceViewer";
import { formatDate } from "../../shared/ui";
import { useState } from "react";
import { SkillVersions } from "./SkillVersions";

export function SkillCard({
  api,
  skill,
  busy,
  decide,
  refresh,
}: {
  api: LingApi;
  skill: Skill;
  busy: boolean;
  decide: (action: SkillAction) => void;
  refresh: () => Promise<void>;
}) {
  const [versionsOpen, setVersionsOpen] = useState(false);
  return (
    <article className="panel management-card">
      <div className="section-heading">
        <h3>{skill.name}</h3>
        <span className="pill">
          {skillLabels[skill.status] ?? skill.status}
        </span>
      </div>
      <p className="metadata">
        版本 {skill.revision} · {formatDate(skill.created_at)}
      </p>
      <p className="management-copy">{skill.description}</p>
      <details>
        <summary>查看技能方法</summary>
        <p className="management-copy">{skill.instructions}</p>
      </details>
      <Sources api={api} sources={skill.sources} />
      {skill.status === "stale" && (
        <p className="inline-warning">
          依据已失效，需重新核对来源后再提交新的技能版本。
        </p>
      )}
      <div className="button-row">
        {["candidate", "disabled", "rejected"].includes(skill.status) && (
          <button
            className="button primary"
            disabled={busy}
            onClick={() => decide("activate")}
          >
            启用技能
          </button>
        )}
        {skill.status === "candidate" && (
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => decide("reject")}
          >
            拒绝技能
          </button>
        )}
        {skill.status === "active" && (
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => decide("disable")}
          >
            停用技能
          </button>
        )}
      </div>
      <details
        open={versionsOpen}
        onToggle={(event) => setVersionsOpen(event.currentTarget.open)}
      >
        <summary>查看技能版本</summary>
        {versionsOpen && (
          <SkillVersions api={api} skill={skill} refresh={refresh} />
        )}
      </details>
    </article>
  );
}
