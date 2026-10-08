import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { useResource } from "../../shared/useResource";
import { EmptyState, ErrorNotice, formatDate } from "../../shared/ui";
import { Sources } from "../sources/SourceViewer";
import { ResourceNotice } from "../tasks/ManagementUi";
import { useManagementAction } from "../tasks/useManagementAction";
import { skillsApi, skillLabels } from "./api";
import type { Skill, SkillVersion } from "./api";

export function SkillVersions({
  api,
  skill,
  refresh,
}: {
  api: LingApi;
  skill: Skill;
  refresh: () => Promise<void>;
}) {
  const load = useCallback(
    (signal: AbortSignal) => skillsApi(api).versions(skill.id, signal),
    [api, skill.id, skill.revision],
  );
  const resource = useResource(load);
  const action = useManagementAction(async () => {
    await Promise.all([refresh(), resource.refresh()]);
  });
  const [target, setTarget] = useState<SkillVersion | null>(null);
  return (
    <div className="stack">
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {resource.data?.length === 0 && (
        <EmptyState title="没有版本记录">
          服务尚未返回这个技能的历史版本。
        </EmptyState>
      )}
      {action.error && <ErrorNotice message={action.error} />}
      {resource.data?.map((version) => (
        <section key={version.revision} className="panel management-card">
          <p className="metadata">
            版本 {version.revision} ·{" "}
            {skillLabels[version.status] ?? version.status} ·{" "}
            {formatDate(version.created_at)}
          </p>
          <p className="management-copy">{version.instructions}</p>
          <Sources api={api} sources={version.sources} />
          {version.revision < skill.revision &&
            ["active", "candidate"].includes(version.status) && (
              <button
                className="button secondary"
                disabled={action.busy}
                onClick={() => setTarget(version)}
              >
                恢复到版本 {version.revision}
              </button>
            )}
        </section>
      ))}
      {target && (
        <ConfirmDialog
          title={"恢复技能版本 " + target.revision}
          confirmLabel="确认恢复技能"
          busy={action.busy}
          onCancel={() => setTarget(null)}
          onConfirm={() =>
            void action.run(async () => {
              await skillsApi(api).restore(skill, target.revision);
              setTarget(null);
            })
          }
        >
          <p>
            恢复「{skill.name}」的版本 {target.revision}
            ，会创建新的版本，已有历史记录会保留。服务会重新核对该版本的来源。
          </p>
          <p className="management-copy">{target.instructions}</p>
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </div>
  );
}
