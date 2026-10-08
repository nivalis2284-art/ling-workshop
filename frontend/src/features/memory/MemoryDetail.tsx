import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { ApiError, errorMessage, type LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice, formatDate, Loading } from "../../shared/ui";
import { useResource } from "../../shared/useResource";
import { Sources } from "../sources/SourceViewer";
import {
  kindLabels,
  memoryApi,
  scopeLabels,
  statusLabels,
  type MemoryAction,
  type MemoryItem,
  type MemoryKind,
} from "./api";

type DetailProps = {
  api: LingApi;
  kind: MemoryKind;
  initial: MemoryItem;
  back: () => void;
  deleted: () => void;
};

export function MemoryDetail(props: DetailProps) {
  const { api, kind, initial, back } = props;
  const load = useCallback(
    (signal: AbortSignal) => memoryApi(api).read(kind, initial.id, signal),
    [api, kind, initial.id],
  );
  const resource = useResource(load);
  if (resource.status === "ready")
    return <MemoryDetailContent {...props} initial={resource.data} />;
  return (
    <section className="panel memory-detail" aria-label="记忆详情">
      <button className="text-button back-link" onClick={back}>
        返回记忆列表
      </button>
      {resource.status === "loading" ? (
        <Loading label="正在读取记忆详情…" />
      ) : (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
    </section>
  );
}

function MemoryDetailContent({
  api,
  kind,
  initial,
  back,
  deleted,
}: DetailProps) {
  const [item, setItem] = useState(initial);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(initial.title ?? "");
  const [content, setContent] = useState(
    initial.summary ?? initial.statement ?? "",
  );
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const fieldId = useId();
  const client = memoryApi(api);
  const disabled = busy || conflict;
  const valid = item.source_valid && item.status !== "invalidated";

  async function run(operation: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    setMessage("");
    try {
      await operation();
    } catch (failure) {
      setError(errorMessage(failure));
      if (failure instanceof ApiError && failure.status === 409)
        setConflict(true);
      setConfirmDelete(false);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  function update(updated: MemoryItem) {
    if (mounted.current) setItem(updated);
  }

  function state(action: MemoryAction) {
    void run(async () => {
      const result = await client.state(kind, item, action);
      if (result.status === "deleted") {
        if (mounted.current) deleted();
        return;
      }
      update(result);
      setMessage(action === "archive" ? "已归档" : "已恢复使用");
      setEditing(false);
    });
  }

  return (
    <section
      className="panel memory-detail"
      aria-label="记忆详情"
      aria-busy={busy}
    >
      <button className="text-button back-link" onClick={back} disabled={busy}>
        <ArrowLeft size={15} aria-hidden="true" /> 返回记忆列表
      </button>
      <div className="memory-tags">
        <span className="pill">{scopeLabels[item.scope]}</span>
        <span className="pill">{statusLabels[item.status]}</span>
        {item.kind && (
          <span className="pill">{kindLabels[item.kind] ?? "知识记录"}</span>
        )}
        {item.provenance === "owner_assertion" && (
          <span className="pill">主人更正</span>
        )}
      </div>
      <h2 className="episode-title">
        {kind === "episodes" ? item.title : "知识主张"}
      </h2>
      <p className="metadata">
        版本 {item.revision}
        {item.created_at && " · " + formatDate(item.created_at)}
      </p>
      <p className="muted small">
        更正仅记录主人的新主张；原始记录与来源仍保留。整理的认识仍可核对和修正。
      </p>
      {item.source_notice && <p className="notice">{item.source_notice}</p>}
      {error && <ErrorNotice message={error} />}
      {conflict && (
        <button
          className="button secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              const latest = await client.read(kind, item.id);
              update(latest);
              setConflict(false);
              setEditing(false);
              setTitle(latest.title ?? "");
              setContent(latest.summary ?? latest.statement ?? "");
            })
          }
        >
          重新读取记忆
        </button>
      )}
      {message && <p role="status">{message}</p>}
      {editing ? (
        <form
          className="memory-editor"
          onSubmit={(event) => {
            event.preventDefault();
            if (disabled || !valid) return;
            void run(async () => {
              const changes =
                kind === "episodes"
                  ? { title: title.trim(), summary: content.trim() }
                  : { statement: content.trim() };
              update(await client.correct(kind, item, changes));
              setEditing(false);
              setMessage("已保存更正");
            });
          }}
        >
          {kind === "episodes" && (
            <label htmlFor={fieldId + "-title"}>
              记忆标题
              <input
                id={fieldId + "-title"}
                value={title}
                maxLength={500}
                disabled={busy}
                onChange={(event) => setTitle(event.target.value)}
                required
              />
            </label>
          )}
          <label htmlFor={fieldId + "-content"}>
            {kind === "episodes" ? "记忆摘要" : "知识主张"}
            <textarea
              id={fieldId + "-content"}
              aria-label={kind === "episodes" ? "记忆摘要" : "知识主张"}
              value={content}
              maxLength={20000}
              rows={7}
              disabled={busy}
              onChange={(event) => setContent(event.target.value)}
              required
            />
          </label>
          <div className="button-row">
            <button
              className="button primary"
              type="submit"
              disabled={
                disabled ||
                !valid ||
                !content.trim() ||
                (kind === "episodes" && !title.trim())
              }
            >
              保存更正
            </button>
            <button
              className="button secondary"
              type="button"
              disabled={busy}
              onClick={() => setEditing(false)}
            >
              取消更正
            </button>
          </div>
        </form>
      ) : (
        <p className="episode-summary">{item.summary ?? item.statement}</p>
      )}
      <div className="button-row memory-actions">
        <button
          className="button secondary"
          disabled={disabled || !valid || editing}
          onClick={() => {
            setTitle(item.title ?? "");
            setContent(item.summary ?? item.statement ?? "");
            setMessage("");
            setEditing(true);
          }}
        >
          更正
        </button>
        {item.status === "active" && (
          <button
            className="button secondary"
            disabled={disabled || editing}
            onClick={() => state("archive")}
          >
            归档
          </button>
        )}
        {item.status === "archived" && (
          <button
            className="button secondary"
            disabled={disabled || !valid || editing}
            onClick={() => state("restore")}
          >
            恢复使用
          </button>
        )}
        <button
          className="button secondary"
          disabled={disabled || editing}
          onClick={() => setConfirmDelete(true)}
        >
          删除
        </button>
      </div>
      <Sources api={api} sources={item.sources} />
      {item.corrections.length > 0 && (
        <details className="memory-history">
          <summary>更正历史</summary>
          <ol>
            {item.corrections.map((edit) => (
              <li key={edit.revision}>
                <p className="metadata">
                  主人更正 · 版本 {edit.revision} ·{" "}
                  {formatDate(edit.created_at)}
                </p>
                {edit.prior.title && <p>原标题：{edit.prior.title}</p>}
                {edit.current.title && <p>更正标题：{edit.current.title}</p>}
                <p className="field-label">更正前</p>
                <blockquote>
                  {edit.prior.summary ?? edit.prior.statement}
                </blockquote>
                <p className="field-label">更正后</p>
                <blockquote>
                  {edit.current.summary ?? edit.current.statement}
                </blockquote>
              </li>
            ))}
          </ol>
        </details>
      )}
      {confirmDelete && (
        <ConfirmDialog
          title="删除这条记忆？"
          confirmLabel="确认删除"
          busy={busy}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => state("delete")}
        >
          <p>
            将删除这条整理后的记忆及其来源关联。原始交流、资料记录与更正历史仍保留。
          </p>
          <p>
            归档可以暂时停用，之后在来源仍有效时恢复；删除后无法在这里恢复。
          </p>
        </ConfirmDialog>
      )}
    </section>
  );
}
