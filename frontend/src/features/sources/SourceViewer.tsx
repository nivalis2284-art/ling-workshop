import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, FileText, X } from "lucide-react";
import type { LingApi } from "../../api/client";
import type { SourceRef } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, formatDate, Loading } from "../../shared/ui";

function SourceContent({
  api,
  source,
  onClose,
}: {
  api: LingApi;
  source: SourceRef;
  onClose: () => void;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.source(source, signal),
    [api, source],
  );
  const resource = useResource(load);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const kinds: Record<string, string> = {
    user: "你的记录",
    assistant: "灵的记录",
    tool: "工具记录",
  };
  return (
    <section className="source-view" aria-label="来源原文">
      <div className="section-heading">
        <h3 ref={heading} tabIndex={-1}>
          <FileText size={18} aria-hidden="true" /> 来源原文
        </h3>
        <button className="icon-button" aria-label="关闭来源" onClick={onClose}>
          <X size={18} />
        </button>
      </div>
      <p className="metadata">
        版本 {source.revision} ·{" "}
        <span className="break-anywhere">{source.id}</span>
      </p>
      {resource.status === "loading" && <Loading label="正在读取来源原文…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          <p className="metadata">
            {kinds[resource.data.kind ?? ""] ?? "来源记录"}
            {resource.data.recorded_at &&
              " · " + formatDate(resource.data.recorded_at)}
          </p>
          <blockquote className="source-text">{resource.data.text}</blockquote>
        </>
      )}
    </section>
  );
}

export function Sources({
  api,
  sources,
}: {
  api: LingApi;
  sources: SourceRef[];
}) {
  const [selected, setSelected] = useState<SourceRef | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  return (
    <div className="sources">
      <p className="field-label">依据与来源</p>
      {sources.length === 0 ? (
        <p className="muted small">这条资料没有提供来源。</p>
      ) : (
        <div className="source-links">
          {sources.map((source, index) => (
            <button
              key={source.id + ":" + source.revision}
              className="source-link"
              aria-label={
                "查看来源 " + (index + 1) + "，版本 " + source.revision
              }
              aria-expanded={selected === source}
              onClick={(event) => {
                trigger.current = event.currentTarget;
                setSelected(source);
              }}
            >
              <FileText size={14} aria-hidden="true" /> 来源 {index + 1}
              <span>v{source.revision}</span>
              <ArrowUpRight size={14} aria-hidden="true" />
            </button>
          ))}
        </div>
      )}
      {selected && (
        <SourceContent
          key={selected.id + ":" + selected.revision}
          api={api}
          source={selected}
          onClose={() => {
            setSelected(null);
            trigger.current?.focus();
          }}
        />
      )}
    </div>
  );
}
