import { ArrowRight, Files, Globe, MessageCircle } from "lucide-react";
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
      body: "让灵读文件、改文件、跑命令都在这里完成，多步工作也不用另外建任务。",
      icon: MessageCircle,
    },
    {
      page: "content",
      title: "资料",
      body: "上传文件作为可查阅的资料，并在对话中把它们发给灵。",
      icon: Files,
    },
    {
      page: "browser",
      title: "浏览器",
      body: "让灵浏览网页，并在执行敏感操作前请你确认。",
      icon: Globe,
    },
  ];
  return (
    <div className="page">
      <PageHeader
        title={agent.name + "的空间"}
        description="从一次真实的对话开始，把资料和任务交给灵来处理。"
      />
      <section className="hero">
        <span className="pill">
          <span className="status-dot" />
          本地空间
        </span>
        <h2>在这里，和灵一起做事。</h2>
        <p>对话、资料与网页浏览都在这台电脑上完成，不需要把内容送到别处。</p>
        <button className="button primary" onClick={() => navigate("chat")}>
          开始对话 <ArrowRight size={15} aria-hidden="true" />
        </button>
      </section>
      <div className="overview-grid">
        {entries.map((entry) => (
          <section className="panel overview-card" key={entry.page}>
            <div className="card-icon">
              <entry.icon size={18} strokeWidth={1.6} />
            </div>
            <h3>{entry.title}</h3>
            <p className="muted">{entry.body}</p>
            <button className="text-button" onClick={() => navigate(entry.page)}>
              前往{entry.title} <ArrowRight size={14} aria-hidden="true" />
            </button>
          </section>
        ))}
      </div>
      <section className="panel readiness">
        <div className="section-heading">
          <h3>数据与设置</h3>
          <span className="pill">本地优先</span>
        </div>
        <p className="readiness-note">
          资料保存在这台电脑上。模型连接、权限与备份都可以在设置中调整。
        </p>
        <button className="text-button" onClick={() => navigate("settings")}>
          打开设置 <ArrowRight size={14} aria-hidden="true" />
        </button>
      </section>
    </div>
  );
}
