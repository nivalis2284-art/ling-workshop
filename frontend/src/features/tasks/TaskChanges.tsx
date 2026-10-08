import { useState } from "react";
import type { LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { EmptyState, ErrorNotice } from "../../shared/ui";
import { downloadArtifact, tasksApi } from "./api";
import type { ChangeReview, Task } from "./api";
import { useManagementAction } from "./useManagementAction";

export function TaskChanges({
  api,
  task,
  onApplied,
}: {
  api: LingApi;
  task: Task;
  onApplied: () => Promise<void>;
}) {
  const [review, setReview] = useState<ChangeReview | null>(null);
  const [confirm, setConfirm] = useState<ChangeReview | null>(null);
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
              setReview(await tasksApi(api).changes(task.id));
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
            </article>
          ))}
          {review.review_id && review.changes.length > 0 && (
            <>
              <p className="muted small">
                删除和二进制改动需要手动处理。项目或任务副本在审阅后发生变化时，服务会拒绝写回。
              </p>
              <button
                className="button primary"
                disabled={action.busy || active || Boolean(message)}
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
              const result = await tasksApi(api).apply(task.id, review_id);
              if (result.status !== "applied")
                throw new Error("服务没有确认改动已写回，请检查执行记录。");
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
