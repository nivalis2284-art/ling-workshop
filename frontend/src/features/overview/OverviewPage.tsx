import { ArrowRight, Files, Globe, ListTodo, MessageCircle, Sprout } from "lucide-react";
import type { Agent } from "../../api/types";
import type { PageId } from "../../app/navigation";
import { PageHeader } from "../../shared/ui";

export function OverviewPage({
  agent,
  navigate,
}: {
  agent: Agent;
  navigate: (page: PageId) => void;
}) {
  const entries: { page: PageId; title: string; body: string; icon: typeof Files }[] = [
    {
      page: "chat",
      title: "对话",
      body: "和灵聊一聊。消息会保存在本地服务，随时可以继续或重新生成。",
      icon: MessageCircle,
    },
    {
      page: "content",
      title: "资料",
      body: "上传文件作为可查阅的资料，并在对话中把它们发给灵。",
      icon: Files,
    },
    {
      page: "tasks",
      title: "任务",
      body: "把多步工作交给灵执行，随时查看进度与产物。",
      icon: ListTodo,
    },
    {
      page: "browser",
      title: "网页",
      body: "让灵浏览网页，并在执行敏感操作前请你确认。",
      icon: Globe,
    },
  ];
  return (
    <div className="page">
      <PageHeader
        eyebrow="一个能独立运行的空间"
        title={agent.name + "的空间"}
        description="从一次真实的对话开始，把资料和任务交给灵来处理。"
      />
      <section className="hero panel">
        <div className="hero-copy">
          <span className="pill">
            <span className="status-dot" /> 本地空间
          </span>
          <h2>
            在这里，
            <br />
            和灵一起做事。
          </h2>
          <p>
            对话、资料、任务与网页浏览都在这台电脑上完成，
            <br className="desktop-break" />
            不需要把内容送到别处。
          </p>
          <button className="button primary" onClick={() => navigate("chat")}>
            开始对话 <ArrowRight size={16} aria-hidden="true" />
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
          <span className="art-caption">一点一滴，从对话开始</span>
        </div>
      </section>
      <div className="overview-grid">
        {entries.map((entry) => (
          <section className="panel overview-card" key={entry.page}>
            <div className="card-icon">
              <entry.icon size={21} strokeWidth={1.5} />
            </div>
            <div className="section-heading">
              <h2>{entry.title}</h2>
            </div>
            <p className="muted">{entry.body}</p>
            <button className="text-button" onClick={() => navigate(entry.page)}>
              前往{entry.title} <ArrowRight size={15} />
            </button>
          </section>
        ))}
      </div>
      <section className="readiness panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">本地优先</p>
            <h2>数据与设置</h2>
          </div>
        </div>
        <p className="readiness-note">
          资料保存在这台电脑上。模型连接、权限与备份都可以在设置中调整。
        </p>
        <button className="text-button" onClick={() => navigate("settings")}>
          打开设置 <ArrowRight size={15} />
        </button>
      </section>
    </div>
  );
}
