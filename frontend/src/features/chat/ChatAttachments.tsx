import type {
  Attachment,
  ModelCapabilities,
  ModelProfile,
} from "../../api/types";
import { ErrorNotice, Loading } from "../../shared/ui";

export function attachmentProblem(
  ids: string[],
  attachments: Attachment[],
  capabilities?: ModelCapabilities,
  apiFamily?: ModelProfile["api_family"],
): string | undefined {
  if (ids.length > 16) return "每条消息最多可附 16 个文件，请减少选择。";
  for (const id of ids) {
    const attachment = attachments.find((item) => item.id === id);
    if (!attachment)
      return "正在读取所选附件的信息；读取失败时请重新加载附件。";
    if (
      /\.(png|jpe?g|webp)$/i.test(attachment.original_name) &&
      capabilities?.images !== true
    )
      return "当前模型未声明图片能力，请在模型设置中启用图片输入或更换模型。";
    if (
      /\.pdf$/i.test(attachment.original_name) &&
      (attachment.status === "needs_vision" ||
        (Array.isArray(attachment.metadata.pages_needing_vision) &&
          attachment.metadata.pages_needing_vision.length > 0))
    ) {
      if (apiFamily === "openai_chat_completions")
        return "当前接口不支持扫描 PDF 的原生读取，请选择支持 PDF 输入的接口类型。";
      if (capabilities?.pdf !== true)
        return "当前模型未声明 PDF 能力，无法读取扫描或含图片的页面。请启用 PDF 输入或更换模型。";
    }
  }
}

export function AttachmentNames({
  ids,
  attachments,
}: {
  ids: string[];
  attachments: Attachment[];
}) {
  if (!ids.length) return null;
  return (
    <ul className="chat-attached-names" aria-label="消息附件">
      {ids.map((id) => (
        <li key={id}>
          {attachments.find((item) => item.id === id)?.original_name ??
            "已附文件"}
        </li>
      ))}
    </ul>
  );
}

export function ChatAttachments({
  attachments,
  selected,
  onSelect,
  disabled,
  loading,
  error,
  refresh,
  upload,
  uploading,
  uploadError,
  problem,
}: {
  attachments: Attachment[];
  selected: string[];
  onSelect: (ids: string[]) => void;
  disabled: boolean;
  loading: boolean;
  error?: string;
  refresh: () => void;
  upload: (file: File) => void;
  uploading: boolean;
  uploadError?: string;
  problem?: string;
}) {
  return (
    <div className="chat-attachment-picker">
      <details>
        <summary>
          附件{selected.length ? "（已选 " + selected.length + "）" : ""}
        </summary>
        <p className="metadata">
          选择已保存的文件，或上传后随这条消息发送。文档文字会自动提取；图片和扫描
          PDF 需要模型支持。
        </p>
        <div className="chat-attachment-tools">
          <label htmlFor="chat-upload">上传聊天附件</label>
          <input
            id="chat-upload"
            type="file"
            accept=".txt,.md,.csv,.docx,.xlsx,.pdf,.png,.jpg,.jpeg,.webp"
            disabled={disabled || uploading}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) upload(file);
            }}
          />
          <button
            type="button"
            className="text-button"
            disabled={disabled || loading || uploading}
            onClick={refresh}
          >
            刷新附件
          </button>
        </div>
        {loading && <Loading label="正在读取附件…" />}
        {uploading && <Loading label="正在上传并读取文件…" />}
        {error && <ErrorNotice message={error} />}
        {uploadError && <ErrorNotice message={uploadError} />}
        {!attachments.length && !loading && !error && (
          <p className="metadata">还没有保存的附件，可以先上传文件。</p>
        )}
        <div className="chat-attachment-list" aria-label="可选附件">
          {attachments.map((attachment) => (
            <div className="chat-attachment-option" key={attachment.id}>
              <label>
                <input
                  type="checkbox"
                  aria-label={"选择 " + attachment.original_name}
                  disabled={disabled || uploading}
                  checked={selected.includes(attachment.id)}
                  onChange={(event) =>
                    onSelect(
                      event.target.checked
                        ? [...selected, attachment.id]
                        : selected.filter((id) => id !== attachment.id),
                    )
                  }
                />
                <span>{attachment.original_name}</span>
                <span className="metadata">
                  {attachment.status === "partial"
                    ? "部分提取"
                    : attachment.status === "needs_vision"
                      ? "需要视觉读取"
                      : "已提取文字"}
                </span>
              </label>
              {attachment.warnings.length > 0 && (
                <ul className="chat-attachment-warnings metadata">
                  {attachment.warnings.map((warning, index) => (
                    <li key={index}>{warning}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      </details>
      {selected.length > 0 && (
        <>
          <AttachmentNames ids={selected} attachments={attachments} />
          <button
            type="button"
            className="text-button"
            disabled={disabled || uploading}
            onClick={() => onSelect([])}
          >
            清空附件选择
          </button>
        </>
      )}
      {problem && <p className="notice">{problem}</p>}
    </div>
  );
}
