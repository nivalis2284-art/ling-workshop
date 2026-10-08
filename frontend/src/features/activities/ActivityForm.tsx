import { useState } from "react";
import type { LingApi } from "../../api/client";
import { ErrorNotice } from "../../shared/ui";
import { activitiesApi, defaultDue, kindLabels, watchOrigin } from "./api";
import type { ActivityKind, WebOrigins } from "./api";
import { useManagementAction } from "../tasks/useManagementAction";

export function ActivityForm({
  api,
  agentId,
  origins,
  refresh,
}: {
  api: LingApi;
  agentId: string;
  origins: WebOrigins;
  refresh: () => Promise<void>;
}) {
  const [kind, setKind] = useState<ActivityKind>("reminder");
  const [title, setTitle] = useState("");
  const [target, setTarget] = useState("");
  const [due, setDue] = useState(defaultDue);
  const [minutes, setMinutes] = useState(15);
  const [repeat, setRepeat] = useState(false);
  const [saved, setSaved] = useState(false);
  const action = useManagementAction(refresh);
  const watch = kind === "file_watch" || kind === "web_watch";
  return (
    <section className="panel section-panel">
      <h2>创建活动</h2>
      <form
        className="management-form"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(async () => {
            if (!title.trim()) throw new Error("请填写活动标题。");
            const date = new Date(due);
            if (Number.isNaN(date.getTime()))
              throw new Error("请选择首次执行时间。");
            const recurring = kind !== "reminder" || repeat;
            if (
              recurring &&
              (!Number.isInteger(minutes) || minutes < 1 || minutes > 5256000)
            )
              throw new Error("执行间隔须为 1 至 5256000 分钟的整数。");
            if (watch && !target.trim())
              throw new Error("请填写具体的关注目标。");
            if (
              kind === "web_watch" &&
              !origins.origins.includes(watchOrigin(target.trim()))
            )
              throw new Error(
                "请先在网页关注授权中允许这个网站来源，再创建关注。",
              );
            await activitiesApi(api).create({
              agent_id: agentId,
              kind,
              title: title.trim(),
              ...(watch ? { target: target.trim() } : {}),
              due_at: date.toISOString(),
              interval_seconds: recurring ? minutes * 60 : null,
            });
            setTitle("");
            setTarget("");
            setSaved(true);
          });
        }}
      >
        <fieldset disabled={action.busy}>
          <div className="management-fields">
            <label>
              活动类型
              <select
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value as ActivityKind);
                  setSaved(false);
                }}
              >
                {Object.entries(kindLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              活动标题
              <input
                required
                maxLength={200}
                value={title}
                onChange={(event) => {
                  setTitle(event.target.value);
                  setSaved(false);
                }}
              />
            </label>
          </div>
          {watch && (
            <label>
              关注目标
              <input
                required
                maxLength={2048}
                value={target}
                onChange={(event) => setTarget(event.target.value)}
                placeholder={
                  kind === "web_watch"
                    ? "https://example.com/releases"
                    : "H:/我的项目/周报.md"
                }
              />
            </label>
          )}
          {kind === "file_watch" && (
            <p className="muted small">
              请填写已存在文件的完整路径，并先在设置中授予该目录读取权限。
            </p>
          )}
          {kind === "topic_review" && (
            <p className="muted small">
              将要复查的主题写在标题中。复查依据实际发生的任务和工具结果，由服务决定是否产生通知。
            </p>
          )}
          <label>
            首次执行时间
            <input
              type="datetime-local"
              required
              value={due}
              onChange={(event) => setDue(event.target.value)}
            />
          </label>
          {kind === "reminder" && (
            <label className="check-label">
              <input
                type="checkbox"
                checked={repeat}
                onChange={(event) => setRepeat(event.target.checked)}
              />
              重复提醒
            </label>
          )}
          {(kind !== "reminder" || repeat) && (
            <label>
              执行间隔（分钟）
              <input
                type="number"
                required
                min={1}
                max={5256000}
                step={1}
                value={minutes}
                onChange={(event) => setMinutes(Number(event.target.value))}
              />
            </label>
          )}
          <button className="button primary">
            {action.busy ? "正在创建…" : "创建活动"}
          </button>
        </fieldset>
        {action.error && <ErrorNotice message={action.error} />}
        {saved && (
          <p role="status" className="management-status">
            活动已创建。
          </p>
        )}
      </form>
    </section>
  );
}
