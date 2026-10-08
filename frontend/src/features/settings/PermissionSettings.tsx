import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent, AgentPermissions } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";
import "./management.css";

function pathLines(value: string) {
  return value
    .split(/\r?\n/)
    .map((path) => path.trim())
    .filter(Boolean);
}

function PermissionEditor({
  api,
  initial,
  reload,
}: {
  api: LingApi;
  initial: AgentPermissions;
  reload: () => Promise<void>;
}) {
  const [saved, setSaved] = useState(initial);
  const [readable, setReadable] = useState(initial.readable.join("\n"));
  const [writable, setWritable] = useState(initial.writable.join("\n"));
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const dirty =
    JSON.stringify(pathLines(readable)) !== JSON.stringify(saved.readable) ||
    JSON.stringify(pathLines(writable)) !== JSON.stringify(saved.writable);

  async function save() {
    if (busy || conflict || !dirty) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const result = await api.savePermissions(
        saved.agent_id,
        {
          readable: pathLines(readable),
          writable: pathLines(writable),
          expected_revision: saved.revision,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setSaved(result);
      setReadable(result.readable.join("\n"));
      setWritable(result.writable.join("\n"));
      setSuccess("权限已保存 · 版本 " + result.revision);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        setConflict(true);
        setError(
          "保存未完成：权限版本可能已变化或路径无法访问。请重新读取权限，检查后再保存。" +
            errorMessage(error),
        );
      } else {
        setError(
          error instanceof ApiError && error.status === 422
            ? "权限未保存，请检查路径是否完整、存在且可访问。" +
                errorMessage(error)
            : errorMessage(error),
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <p className="metadata">权限版本 · {saved.revision}</p>
      <div className="permission-paths">
        <div>
          <label htmlFor="permission-readable">可读取路径</label>
          <textarea
            id="permission-readable"
            rows={4}
            value={readable}
            disabled={busy}
            placeholder="例如 C:/LingData/参考资料"
            aria-describedby="permission-path-help"
            onChange={(event) => {
              setReadable(event.target.value);
              setSuccess("");
            }}
          />
        </div>
        <div>
          <label htmlFor="permission-writable">可写入路径</label>
          <textarea
            id="permission-writable"
            rows={4}
            value={writable}
            disabled={busy}
            placeholder="例如 D:/LingData/产物"
            aria-describedby="permission-path-help"
            onChange={(event) => {
              setWritable(event.target.value);
              setSuccess("");
            }}
          />
        </div>
      </div>
      <p id="permission-path-help" className="field-help">
        每行填写一个完整目录路径。读取与写入分别授权；可写入不代表可读取。清空某栏并保存会撤销该栏的授权。
        路径有效性由本地服务检查，仅在点击“保存权限”后更新。
      </p>
      {error && <ErrorNotice message={error} />}
      {success && (
        <p className="notice success" role="status">
          {success}
        </p>
      )}
      <div className="button-row">
        <button
          className="button primary"
          disabled={busy || conflict || !dirty}
        >
          {busy ? "正在保存权限…" : "保存权限"}
        </button>
        <button
          className="button secondary"
          type="button"
          disabled={busy}
          onClick={() => void reload()}
        >
          重新读取权限
        </button>
      </div>
      <p className="field-help">重新读取会用已保存的权限替换当前草稿。</p>
    </form>
  );
}

export function PermissionSettings({
  api,
  agent,
}: {
  api: LingApi;
  agent: Agent;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.permissions(agent.id, signal),
    [api, agent.id],
  );
  const resource = useResource(load);
  return (
    <section
      className="panel section-panel permission-settings"
      aria-labelledby="permissions-heading"
    >
      <div className="section-heading">
        <h2 id="permissions-heading">文件访问权限</h2>
        <span className="pill">{agent.name}</span>
      </div>
      <p className="muted">由你明确指定这位灵可以读取和写入的本地目录。</p>
      {resource.status === "loading" && <Loading label="正在读取权限…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <PermissionEditor
          key={agent.id}
          api={api}
          initial={resource.data}
          reload={resource.refresh}
        />
      )}
    </section>
  );
}
