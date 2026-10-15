import { useState } from "react";
import type { LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { EmptyState, ErrorNotice } from "../../shared/ui";
import { downloadArtifact, tasksApi } from "./api";
import type { ApplyResult, ChangeReview, Task } from "./api";
import { useManagementAction } from "./useManagementAction";

type ReviewedChange = ChangeReview["changes"][number] & {
  baseline_artifact?: string | null;
};
type DetailedReview = Omit<ChangeReview, "changes"> & { changes: ReviewedChange[] };
type DetailedApplyResult = ApplyResult & {
  files?: {
    path: string;
    status: "applied" | "not_applied" | "conflict" | "unknown";
    reason?: string;
    effect_id?: string;
  }[];
};

export function TaskChanges({
  api,
  task,
  onApplied,
}: {
  api: LingApi;
  task: Task;
  onApplied: () => Promise<void>;
}) {
  const [review, setReview] = useState<DetailedReview | null>(null);
  const [applyResult, setApplyResult] = useState<DetailedApplyResult | null>(null);
  const [confirm, setConfirm] = useState<DetailedReview | null>(null);
  const [message, setMessage] = useState("");
  const action = useManagementAction();
  const active = task.status === "running" || task.status === "queued";
  return (
    <section className="stack" aria-label="项目改动审阅">
      {active ? (
        <p className="muted small">
          暂停任务后，可以审阅项目副本中的文件改动。
        </p>
      ) : (
        <button
          className="button secondary"
          disabled={action.busy}
          onClick={() =>
            void action.run(async () => {
              setMessage("");
              setApplyResult(null);
              setReview((await tasksApi(api).changes(task.id)) as DetailedReview);
            })
          }
        >
          审阅文件改动
        </button>
      )}
      {action.error && <ErrorNotice message={action.error} />}
      {message && (
        <p className="management-status" role="status">
          {message}
        </p>
      )}
      {review && (
        <div className="stack">
          <h3>已读取的文件改动</h3>
          {review.changes.length === 0 && (
            <EmptyState title="没有文件改动">
              服务没有返回待应用的文件改动。
            </EmptyState>
          )}
          {review.changes.map((change) => (
            <article className="panel management-card" key={change.path}>
              <h3>{change.path}</h3>
              <p className="metadata">
                {{ create: "新增", modify: "修改", delete: "删除" }[
                  change.operation
                ] ?? change.operation}
              </p>
              {change.diff !== null ? (
                <pre>{change.diff}</pre>
              ) : (
                <p className="muted small">
                  这是二进制文件，可以下载后检查内容。
                </p>
              )}
              {change.baseline_artifact && (
                <button
                  className="button secondary"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(async () => {
                      downloadArtifact(
                        await tasksApi(api).artifact(task.id, change.baseline_artifact!),
                        change.path,
                      );
                    })
                  }
                >
                  下载原始基线：{change.path}
                </button>
              )}
              {change.artifact && (
                <button
                  className="button secondary"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(async () => {
                      downloadArtifact(
                        await tasksApi(api).artifact(task.id, change.artifact!),
                        change.artifact!,
                      );
                    })
                  }
                >
                  下载成果：{change.path}
                </button>
              )}
              {applyResult?.files?.find((item) => item.path === change.path) && (
                <p className="management-status" role="status">
                  写回状态：
                  {{
                    applied: "已应用",
                    not_applied: "未应用",
                    conflict: "冲突，未覆盖",
                    unknown: "结果未知，需人工核对",
                  }[
                    applyResult.files.find((item) => item.path === change.path)!.status
                  ] ?? "需核对"}
                  {applyResult.files.find((item) => item.path === change.path)?.reason &&
                    `（${applyResult.files.find((item) => item.path === change.path)?.reason}）`}
                </p>
              )}
            </article>
          ))}
          {applyResult && applyResult.status !== "applied" && (
            <p className="management-status" role="alert">
              写回已停止，服务不会自动重试。请逐项核对原始基线、成果和写回状态；未知项可能已产生副作用。
            </p>
          )}
          {review.review_id && review.changes.length > 0 && (
            <>
              <p className="muted small">
                删除和二进制改动需要手动应用：当前写回接口只安全处理 UTF-8 文本的新增和修改。项目或任务副本在审阅后发生变化时，服务会拒绝写回。
              </p>
              <button
                className="button primary"
                disabled={action.busy || active || Boolean(message) || Boolean(applyResult) || review.changes.some((change) => change.operation === "delete" || change.diff === null)}
                onClick={() => setConfirm(review)}
              >
                应用这些改动
              </button>
            </>
          )}
        </div>
      )}
      {confirm?.review_id && (
        <ConfirmDialog
          title="将改动写回项目"
          confirmLabel="确认写回项目"
          busy={action.busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const review_id = confirm.review_id!;
            void action.run(async () => {
              const result = (await tasksApi(api).apply(
                task.id,
                review_id,
              )) as DetailedApplyResult;
              if (result.status !== "applied") {
                setApplyResult(result);
                setConfirm(null);
                setMessage("写回已停止，请核对逐文件状态和原始基线。");
                return;
              }
              setConfirm(null);
              setMessage("改动已写回项目。");
              await onApplied();
            });
          }}
        >
          <p>这将把你刚才审阅的 {confirm.changes.length} 项改动写入原项目：</p>
          <p className="path-preview">{task.workspace}</p>
          <ul>
            {confirm.changes.map((change) => (
              <li key={change.path}>{change.path}</li>
            ))}
          </ul>
          {action.error && <ErrorNotice message={action.error} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
