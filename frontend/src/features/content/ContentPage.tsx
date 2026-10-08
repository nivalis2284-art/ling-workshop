import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, Upload } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { EmptyState, ErrorNotice, Loading, PageHeader } from "../../shared/ui";
import { AttachmentPreview, attachmentStatus } from "./AttachmentPreview";
import "./content.css";

const MAX_FILE_BYTES = 25 * 1024 * 1024;
function fileError(file: File | null) {
  if (!file) return "";
  if (file.size > MAX_FILE_BYTES)
    return "文件超过 25 MB，请选择较小的文件后再上传。";
  if (file.size === 0) return "文件为空，请选择有内容的文件。";
  return "";
}

export function ContentPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    (signal: AbortSignal) => api.attachments(agent.id, signal),
    [api, agent.id],
  );
  const resource = useResource(load);
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const validation = fileError(file);

  async function upload() {
    if (!file || busy || fileError(file)) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const attachment = await api.uploadAttachment(
        agent.id,
        file,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setSuccess("已保存可查阅");
      setFile(null);
      if (input.current) input.current.value = "";
      setSelected(attachment.id);
      await resource.refresh();
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <div className="page content-page">
      <PageHeader
        eyebrow="保存资料，随时查阅"
        title="资料"
        description={agent.name + "的文件与来源片段"}
      />
      <section className="panel section-panel" aria-labelledby="upload-heading">
        <h2 id="upload-heading">上传资料</h2>
        <p className="muted">
          文件保存后可在此查阅。资料尚未接入对话，当前对话不会自动读取上传内容。
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void upload();
          }}
        >
          <label htmlFor="attachment-file">选择资料文件</label>
          <input
            ref={input}
            id="attachment-file"
            type="file"
            disabled={busy}
            aria-describedby="attachment-file-help"
            aria-invalid={Boolean(validation)}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setError("");
              setSuccess("");
            }}
          />
          <p id="attachment-file-help" className="field-help">
            支持 Word（.docx）、PDF、Excel（.xlsx）、TXT、Markdown、CSV 和
            PNG/JPEG/WebP 图片。 单个文件上限 25
            MB，最终由服务端校验格式与大小。图片及扫描页可能需要视觉识别。
          </p>
          {file && (
            <p className="metadata break-anywhere">
              {file.name} · {file.size.toLocaleString("zh-CN")} 字节
            </p>
          )}
          {(validation || error) && (
            <ErrorNotice message={validation || error} />
          )}
          {success && (
            <p className="notice success" role="status">
              {success}
            </p>
          )}
          <button
            className="button primary"
            disabled={!file || busy || Boolean(validation)}
          >
            <Upload size={16} aria-hidden="true" />
            {busy ? "正在上传…" : "上传资料"}
          </button>
        </form>
      </section>
      <div className="content-columns">
        <section
          className="panel section-panel"
          aria-labelledby="attachment-list-heading"
        >
          <div className="section-heading">
            <h2 id="attachment-list-heading">已保存的资料</h2>
            <button
              className="text-button"
              disabled={busy || resource.status === "loading"}
              onClick={() => void resource.refresh()}
            >
              刷新列表
            </button>
          </div>
          {resource.status === "loading" && (
            <Loading label="正在读取资料列表…" />
          )}
          {resource.status === "error" && (
            <ErrorNotice
              message={resource.error}
              retry={() => void resource.refresh()}
            />
          )}
          {resource.status === "ready" &&
            (resource.data.length === 0 ? (
              <EmptyState title="还没有资料">
                选择一个文件，保存后即可查阅来源片段。
              </EmptyState>
            ) : (
              <>
                {resource.data.length === 100 && (
                  <p className="field-help">当前显示最近 100 份资料。</p>
                )}
                <ul className="attachment-list">
                  {resource.data.map((attachment) => (
                    <li key={attachment.id}>
                      <button
                        className={
                          "attachment-choice" +
                          (selected === attachment.id ? " selected" : "")
                        }
                        aria-pressed={selected === attachment.id}
                        onClick={() => setSelected(attachment.id)}
                      >
                        <FileText size={18} aria-hidden="true" />
                        <span>
                          <strong>{attachment.original_name}</strong>
                          <span className="metadata">
                            {attachmentStatus[attachment.status]} ·{" "}
                            {attachment.byte_count.toLocaleString("zh-CN")} 字节
                          </span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            ))}
        </section>
        {selected ? (
          <AttachmentPreview key={selected} api={api} id={selected} />
        ) : (
          <section className="panel section-panel">
            <EmptyState title="选择资料查看内容">
              可查看提取状态、来源位置和警告，并下载原文件。
            </EmptyState>
          </section>
        )}
      </div>
    </div>
  );
}
