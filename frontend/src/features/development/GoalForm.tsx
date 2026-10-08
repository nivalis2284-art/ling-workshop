import { useRef, useState } from "react";
import type { LingApi } from "../../api/client";
import type { SourceRef } from "../../api/types";
import { ErrorNotice } from "../../shared/ui";
import { useManagementAction } from "../tasks/useManagementAction";
import { developmentApi } from "./api";

export function GoalForm({
  api,
  agentId,
  refresh,
}: {
  api: LingApi;
  agentId: string;
  refresh: () => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [saved, setSaved] = useState(false);
  const request = useRef<{
    title: string;
    sourceId: string;
    goalId: string;
    source?: SourceRef;
  } | null>(null);
  const action = useManagementAction(refresh);
  return (
    <form
      className="management-form"
      onSubmit={(event) => {
        event.preventDefault();
        const value = title.trim();
        if (!value) return;
        if (request.current?.title !== value)
          request.current = {
            title: value,
            sourceId: crypto.randomUUID(),
            goalId: crypto.randomUUID(),
          };
        const pending = request.current;
        void action.run(async () => {
          const service = developmentApi(api);
          const source =
            pending.source ??
            (await service.recordSource(agentId, value, pending.sourceId));
          if (
            typeof source.id !== "string" ||
            !Number.isInteger(source.revision) ||
            source.revision < 1
          )
            throw new Error(
              "来源记录未返回有效版本，目标尚未提交。请重新尝试。",
            );
          pending.source = { id: source.id, revision: source.revision };
          await service.propose(agentId, value, pending.source, pending.goalId);
          request.current = null;
          setTitle("");
          setSaved(true);
        });
      }}
    >
      <label>
        长期目标标题
        <input
          required
          value={title}
          disabled={action.busy}
          onChange={(event) => {
            setTitle(event.target.value);
            setSaved(false);
          }}
          placeholder="例如：持续整理项目知识并核对来源"
        />
      </label>
      <p className="muted small">
        你的目标会先保存为真实记录，再作为候选目标提出。核对依据后，点击启用才会成为进行中的目标。
      </p>
      <button
        className="button primary"
        disabled={action.busy || !title.trim()}
      >
        {action.busy ? "正在保存…" : "提出长期目标"}
      </button>
      {action.error && <ErrorNotice message={action.error} />}
      {saved && (
        <p role="status" className="management-status">
          目标已提出，请在列表中核对来源并启用。
        </p>
      )}
    </form>
  );
}
