import { useRef, useState } from "react";
import type { LingApi } from "../../api/client";
import { ErrorNotice } from "../../shared/ui";
import { tasksApi } from "./api";
import { useManagementAction } from "./useManagementAction";

export function TaskForm({
  api,
  agentId,
  onCreated,
}: {
  api: LingApi;
  agentId: string;
  onCreated: (id: string) => Promise<void>;
}) {
  const [objective, setObjective] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [environment, setEnvironment] = useState<"python" | "node">("python");
  const request = useRef<{ key: string; id: string } | null>(null);
  const action = useManagementAction();
  return (
    <section className="panel section-panel">
      <h2>交给灵一项任务</h2>
      <form
        className="management-form"
        onSubmit={(event) => {
          event.preventDefault();
          const value = {
            agent_id: agentId,
            objective: objective.trim(),
            environment,
            ...(workspace.trim() ? { workspace: workspace.trim() } : {}),
          };
          if (!value.objective) return;
          const key = JSON.stringify(value);
          if (request.current?.key !== key)
            request.current = { key, id: crypto.randomUUID() };
          const request_id = request.current.id;
          void action.run(async () => {
            const task = await tasksApi(api).submit({ ...value, request_id });
            setObjective("");
            request.current = null;
            await onCreated(task.id);
          });
        }}
      >
        <fieldset disabled={action.busy}>
          <label>
            任务目标
            <textarea
              required
              maxLength={32000}
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              placeholder="例如：核对项目说明，整理遗漏并给出依据"
            />
          </label>
          <label>
            项目文件夹（可选）
            <input
              value={workspace}
              onChange={(event) => setWorkspace(event.target.value)}
              placeholder="例如 H:/我的项目"
            />
          </label>
          <label>
            执行环境
            <select
              value={environment}
              onChange={(event) =>
                setEnvironment(event.target.value as "python" | "node")
              }
            >
              <option value="python">Python</option>
              <option value="node">Node.js / TypeScript</option>
            </select>
          </label>
          <p className="muted small">
            项目文件夹需先在设置中授予读写权限。灵在副本中工作，审阅并确认后才写回项目。
          </p>
          <button className="button primary" disabled={!objective.trim()}>
            {action.busy ? "正在提交…" : "创建任务"}
          </button>
        </fieldset>
        {action.error && <ErrorNotice message={action.error} />}
      </form>
    </section>
  );
}
