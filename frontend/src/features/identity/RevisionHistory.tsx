import { RotateCcw } from "lucide-react";
import type { Revision } from "../../api/types";
import { EmptyState, formatDate } from "../../shared/ui";
import {
  identityEntryLabel,
  identityEntryText,
} from "../../shared/identityFormatting";

function reasonLabel(reason?: string) {
  if (reason === "initialize") return "初始状态";
  if (reason?.startsWith("owner_rollback:"))
    return "恢复自版本 " + reason.slice("owner_rollback:".length);
  if (reason?.startsWith("source_corrected:")) return "来源纠正后更新";
  return "身份内容更新";
}

export function RevisionHistory({
  revisions,
  current,
  busy,
  restore,
}: {
  revisions: Revision[];
  current: number;
  busy: boolean;
  restore: (revision: Revision) => void;
}) {
  return (
    <section className="panel section-panel" aria-labelledby="revisions-title">
      <div className="section-heading">
        <h2 id="revisions-title">版本记录</h2>
        <span className="metadata">保留变化的来路</span>
      </div>
      <p className="muted small">
        恢复会生成一个新版本。原有记录仍然保留，来源失效的内容可能无法恢复。
      </p>
      {revisions.length === 0 ? (
        <EmptyState title="没有可用的版本记录">
          服务暂未提供版本记录。
        </EmptyState>
      ) : (
        <ol className="revision-list">
          {[...revisions]
            .sort((a, b) => b.revision - a.revision)
            .map((revision) => (
              <li key={revision.revision}>
                <span className="timeline-dot" aria-hidden="true" />
                <div className="revision-info">
                  <strong>
                    版本 {revision.revision}{" "}
                    {revision.revision === current && (
                      <span className="pill">当前</span>
                    )}
                  </strong>
                  <p>
                    {reasonLabel(revision.reason)} ·{" "}
                    {formatDate(revision.created_at)}
                  </p>
                  <details>
                    <summary>查看此版本内容</summary>
                    {Object.entries(revision.entries).length === 0 ? (
                      <p className="muted">此版本尚无身份内容。</p>
                    ) : (
                      <dl className="entry-list compact">
                        {Object.entries(revision.entries).map(([key, text]) => (
                          <div key={key}>
                            <dt>{identityEntryLabel(key)}</dt>
                            <dd>{identityEntryText(key, text)}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </details>
                </div>
                {revision.revision !== current && (
                  <button
                    className="button secondary small-button"
                    disabled={busy}
                    aria-label={"恢复到版本 " + revision.revision}
                    onClick={() => restore(revision)}
                  >
                    <RotateCcw size={14} aria-hidden="true" />
                    恢复
                  </button>
                )}
              </li>
            ))}
        </ol>
      )}
    </section>
  );
}
