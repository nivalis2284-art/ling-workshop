import type { TaskEvent } from "./api";
import { EmptyState, formatDate } from "../../shared/ui";
import { RecordData } from "./ManagementUi";

const labels: Record<string, string> = {
  status: "状态变化",
  model_started: "开始处理",
  usage: "服务用量记录",
  plan: "处理思路",
  tool_started: "工具开始执行",
  tool_result: "工具执行结果",
  final: "最终答复",
  applied: "文件改动已应用",
};
export function TaskEvents({ events }: { events: TaskEvent[] }) {
  return (
    <section aria-label="任务执行记录">
      <details>
        <summary>实际执行记录（{events.length} 条，点击展开）</summary>
        {events.length === 0 ? (
          <EmptyState title="还没有执行记录">
            这里会展示服务已经记录的事件。
          </EmptyState>
        ) : (
          <ol className="management-list">
            {events.map((event) => (
              <li key={event.id} className="panel management-card">
                <div className="section-heading">
                  <strong>
                    {labels[event.type] ?? "执行记录：" + event.type}
                  </strong>
                  <time className="metadata">
                    {formatDate(event.created_at)}
                  </time>
                </div>
                {typeof event.data.text === "string" && (
                  <p className="management-copy">{event.data.text}</p>
                )}
                {typeof event.data.name === "string" && (
                  <p className="metadata">工具：{event.data.name}</p>
                )}
                <RecordData value={event.data} />
              </li>
            ))}
          </ol>
        )}
      </details>
    </section>
  );
}
