import { useCallback } from "react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { EmptyState, ErrorNotice, PageHeader } from "../../shared/ui";
import { ResourceNotice } from "../tasks/ManagementUi";
import { useManagementResource } from "../tasks/useManagementResource";
import { useManagementAction } from "../tasks/useManagementAction";
import { skillsApi } from "./api";
import { SkillCard } from "./SkillCard";

export function SkillsPage({ api, agent }: { api: LingApi; agent: Agent }) {
  const load = useCallback(
    (signal: AbortSignal) => skillsApi(api).list(agent.id, signal),
    [api, agent.id],
  );
  const resource = useManagementResource(load);
  const action = useManagementAction(resource.refresh);
  const groups = [
    { title: "待审阅的技能", statuses: ["candidate"] },
    { title: "正在使用的技能", statuses: ["active"] },
    { title: "其他技能记录", statuses: ["rejected", "disabled", "stale"] },
  ];
  return (
    <div className="management-page">
      <PageHeader
        eyebrow={agent.name + "的空间"}
        title="技能"
        description="核对灵从实际经历中保存的方法与来源，再决定是否使用。启用技能不会增加文件或网站访问权限。"
        action={
          <button
            className="button secondary"
            onClick={() => void resource.refresh()}
          >
            刷新技能
          </button>
        }
      />
      <ResourceNotice {...resource} hasData={Boolean(resource.data)} />
      {action.error && <ErrorNotice message={action.error} />}
      {resource.data?.length === 0 && (
        <EmptyState title="还没有保存的技能">
          灵在完成任务后保存的方法会出现在这里，供你审阅。
        </EmptyState>
      )}
      {resource.data &&
        groups.map((group) => {
          const items = resource.data!.filter(
            (item) =>
              group.statuses.includes(item.status) ||
              (group.title === "其他技能记录" &&
                !["candidate", "active"].includes(item.status)),
          );
          return (
            items.length > 0 && (
              <section
                className="stack"
                key={group.title}
                aria-label={group.title}
              >
                <h2>{group.title}</h2>
                {items.map((skill) => (
                  <SkillCard
                    key={skill.id}
                    api={api}
                    skill={skill}
                    busy={action.busy}
                    refresh={resource.refresh}
                    decide={(decision) =>
                      void action.run(() =>
                        skillsApi(api).decide(skill, decision),
                      )
                    }
                  />
                ))}
              </section>
            )
          );
        })}
    </div>
  );
}
