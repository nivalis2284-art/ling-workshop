import { useCallback } from "react";
import { ArrowRight, BookOpen, Check, ShieldCheck, Sprout } from "lucide-react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import type { PageId } from "../../app/navigation";
import { upcoming } from "../../app/navigation";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading, PageHeader } from "../../shared/ui";

export function OverviewPage({
  api,
  agent,
  navigate,
}: {
  api: LingApi;
  agent: Agent;
  navigate: (page: PageId) => void;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.identity(agent.id, signal),
    [api, agent.id],
  );
  const resource = useResource(load);
  const pending = resource.data?.proposals.filter(
    (proposal) => proposal.status === "awaiting_owner",
  ).length;
  return (
    <div className="page">
      <PageHeader
        eyebrow="一个持续生长的空间"
        title={agent.name + "的空间"}
        description="从真实的经历开始，把每一次变化交给你来确认。"
      />
      <section className="hero panel">
        <div className="hero-copy">
          <span className="pill">
            <span className="status-dot" /> 本地空间
          </span>
          <h2>
            经历会留下，
            <br />
            理解在生长。
          </h2>
          <p>
            在这里，看看灵如何理解自己，
            <br className="desktop-break" />
            也随时回到那些理解的来处。
          </p>
          <button
            className="button primary"
            onClick={() => navigate("identity")}
          >
            查看身份与变化 <ArrowRight size={16} aria-hidden="true" />
          </button>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="art-core">
            <Sprout strokeWidth={0.8} />
          </div>
          <span className="art-seed seed-one" />
          <span className="art-seed seed-two" />
          <span className="art-caption">一点一滴，成为自己</span>
        </div>
      </section>
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <div className="overview-grid">
          <section className="panel overview-card">
            <div className="card-icon">
              <ShieldCheck size={21} strokeWidth={1.5} />
            </div>
            <div className="section-heading">
              <h2>当前的理解</h2>
              <span className="pill">版本 {resource.data.state.revision}</span>
            </div>
            <p className="muted">
              {Object.keys(resource.data.state.entries).length === 0
                ? "身份还在起点。经确认的理解会出现在这里。"
                : "已有 " +
                  Object.keys(resource.data.state.entries).length +
                  " 条身份内容保存在当前版本中。"}
            </p>
            <button
              className="text-button"
              onClick={() => navigate("identity")}
            >
              查看当前身份 <ArrowRight size={15} />
            </button>
          </section>
          <section className="panel overview-card">
            <div className="card-icon warm">
              <BookOpen size={21} strokeWidth={1.5} />
            </div>
            <div className="section-heading">
              <h2>等待你的审阅</h2>
              <span className="pill warm">{pending} 项</span>
            </div>
            <p className="muted">
              {pending
                ? "先查看变化的依据，再决定是否让它成为灵的一部分。"
                : "目前没有待审阅的变化。有了新的候选，会在这里等你。"}
            </p>
            <button
              className="text-button"
              onClick={() => navigate("identity")}
            >
              前往身份审阅 <ArrowRight size={15} />
            </button>
          </section>
        </div>
      )}
      <section className="readiness panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">一步一步，慢慢完善</p>
            <h2>现在可以做什么</h2>
          </div>
          <span className="metadata">功能开放情况</span>
        </div>
        <div className="readiness-grid">
          <div>
            <h3>
              <Check size={16} /> 已接入
            </h3>
            <ul>
              <li>查看身份，批准或拒绝变化</li>
              <li>浏览记忆，追溯来源原文</li>
              <li>查看历史版本，确认后恢复</li>
              <li>导出资料，创建本地备份</li>
            </ul>
          </div>
          <div className="in-progress">
            <h3>
              <Sprout size={17} /> 正在开发
            </h3>
            {upcoming.map((item) => (
              <p key={item.label}>{item.description}</p>
            ))}
            <p>模型连接可在设置中配置，对话入口尚未开放。</p>
          </div>
        </div>
        <p className="readiness-note">
          连接情况以资料读取结果为准；当前页面不代表模型已就绪。
        </p>
      </section>
    </div>
  );
}
