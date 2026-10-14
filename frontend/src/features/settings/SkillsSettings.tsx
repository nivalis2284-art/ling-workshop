import { useCallback } from "react";
import type { LingApi } from "../../api/client";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";

const SOURCE_LABELS: Record<string, string> = {
  project: "项目技能目录",
  user: "用户技能目录",
  bundled: "内置技能（随程序提供）",
};

/** 技能包：从目录自动发现（只读扫描）。技能只是知识，不代表额外权限。 */
export function SkillsSettings({ api }: { api: LingApi }) {
  const load = useCallback(
    (signal: AbortSignal) => api.skillpacks(signal),
    [api],
  );
  const resource = useResource(load);
  return (
    <section className="panel section-panel stack" aria-label="技能包">
      <div className="section-heading">
        <h3>技能包</h3>
        <button
          className="button small-button secondary"
          disabled={resource.status === "loading"}
          onClick={() => void resource.refresh()}
        >
          刷新
        </button>
      </div>
      <p className="muted">
        技能从以下目录自动发现（只读扫描，不执行安装）。技能只是知识，不会带来额外权限——它描述的每个动作仍要走工具、权限与沙箱。
      </p>
      {resource.status === "loading" && <Loading label="正在扫描技能目录…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          <ul className="entry-list">
            {resource.data.roots.map((root) => (
              <div key={root.source}>
                <dt>{SOURCE_LABELS[root.source] ?? root.source}</dt>
                <dd>
                  {root.path}
                  {root.exists ? "" : "（目录不存在）"}
                </dd>
              </div>
            ))}
          </ul>
          {resource.data.skills.length === 0 ? (
            <p className="muted small">还没有发现任何技能。</p>
          ) : (
            <ul className="model-list">
              {resource.data.skills.map((skill) => (
                <li key={skill.name}>
                  <div>
                    <strong>{skill.name}</strong>
                    <p className="metadata">
                      {skill.description || "（无描述）"} · 来源：
                      {SOURCE_LABELS[skill.source] ?? skill.source}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
