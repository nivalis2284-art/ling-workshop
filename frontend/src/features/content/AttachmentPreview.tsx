import { useCallback, useEffect, useRef, useState } from "react";
import { Download } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Attachment } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";

export const attachmentStatus: Record<Attachment["status"], string> = {
  ready: "文本已提取",
  partial: "部分提取",
  needs_vision: "需要视觉识别",
};

function DownloadAttachment({
  api,
  attachment,
}: {
  api: LingApi;
  attachment: Attachment;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function download() {
    if (busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const blob = await api.downloadAttachment(
        attachment.id,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      try {
        link.href = url;
        link.download =
          attachment.original_name.replace(/[\x00-\x1f\\/:*?"<>|]/g, "_") ||
          "attachment";
        document.body.append(link);
        link.click();
      } finally {
        link.remove();
        // Let the browser start consuming the blob before releasing the URL.
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <>
      <button
        className="button secondary"
        disabled={busy}
        onClick={() => void download()}
      >
        <Download size={16} aria-hidden="true" />
        {busy ? "正在下载…" : "下载原文件"}
      </button>
      {error && <ErrorNotice message={error} />}
    </>
  );
}

export function AttachmentPreview({ api, id }: { api: LingApi; id: string }) {
  const load = useCallback(
    (signal: AbortSignal) => api.attachment(id, signal),
    [api, id],
  );
  const resource = useResource(load);
  return (
    <section
      className="panel section-panel attachment-preview"
      aria-label="资料预览"
    >
      {resource.status === "loading" && <Loading label="正在读取资料…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          <div className="section-heading">
            <h2 className="break-anywhere">{resource.data.original_name}</h2>
            <span
              className={
                "pill" + (resource.data.status === "ready" ? "" : " warm")
              }
            >
              {attachmentStatus[resource.data.status]}
            </span>
          </div>
          <p className="metadata">
            {resource.data.byte_count.toLocaleString("zh-CN")} 字节 ·{" "}
            {resource.data.segments.length} 个文本片段
          </p>
          <DownloadAttachment key={id} api={api} attachment={resource.data} />
          {resource.data.status === "partial" && (
            <p className="notice">
              仅提取到部分内容，以下片段不代表完整文件。请结合警告和原文件查阅。
            </p>
          )}
          {resource.data.status === "needs_vision" && (
            <p className="notice">
              尚未识别图片内容，需要视觉识别后才能理解图片或扫描页；保存文件不代表已完成识别。
            </p>
          )}
          {resource.data.warnings.length > 0 && (
            <div className="attachment-warnings">
              <h3>提取警告</h3>
              <ul>
                {resource.data.warnings.map((warning, index) => (
                  <li key={index}>{warning}</li>
                ))}
              </ul>
            </div>
          )}
          <h3 className="attachment-segments-heading">来源片段</h3>
          {resource.data.segments.length === 0 && (
            <p className="muted">暂无可预览的文本片段，可下载原文件查阅。</p>
          )}
          <div className="attachment-segments">
            {resource.data.segments.map((segment, index) => (
              <article key={index}>
                <h4>{segment.location}</h4>
                <p className="preserve-text">{segment.text}</p>
              </article>
            ))}
          </div>
          {Object.keys(resource.data.metadata).length > 0 && (
            <details className="attachment-metadata">
              <summary>文件元数据</summary>
              <pre>{JSON.stringify(resource.data.metadata, null, 2)}</pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}
