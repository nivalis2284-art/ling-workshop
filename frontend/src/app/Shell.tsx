import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { CircleHelp, LockKeyhole } from "lucide-react";
import type { Agent } from "../api/types";
import { Mark } from "../shared/ui";
import { navigation, upcoming } from "./navigation";
import type { PageId } from "./navigation";

export function Shell({
  agents,
  agent,
  page,
  onNavigate,
  onSelectAgent,
  children,
}: {
  agents: Agent[];
  agent: Agent;
  page: PageId;
  onNavigate: (page: PageId) => void;
  onSelectAgent: (id: string) => void;
  children: ReactNode;
}) {
  const main = useRef<HTMLElement>(null);
  const initial = useRef(true);
  useEffect(() => {
    if (initial.current) {
      initial.current = false;
      return;
    }
    main.current?.querySelector<HTMLElement>("h1")?.focus();
  }, [page]);
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <div className="brand">
          <Mark small />
          <div>
            <strong>LingNexus</strong>
            <span>与灵，一起生长</span>
          </div>
        </div>
        <div className="agent-card">
          <span className="avatar" aria-hidden="true">
            {agent.name.slice(0, 1)}
          </span>
          <div>
            <strong>{agent.name}</strong>
            <span>我的灵 · 本地空间</span>
          </div>
        </div>
        {agents.length > 1 && (
          <label className="agent-select">
            切换灵
            <select
              value={agent.id}
              onChange={(event) => onSelectAgent(event.target.value)}
            >
              {agents.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <nav aria-label="主要导航">
          <p className="nav-caption">我的空间</p>
          {navigation.map((item) => (
            <button
              key={item.id}
              className={"nav-item" + (page === item.id ? " active" : "")}
              aria-current={page === item.id ? "page" : undefined}
              onClick={() => onNavigate(item.id)}
            >
              <item.icon size={19} strokeWidth={1.7} aria-hidden="true" />
              {item.label}
              {page === item.id && (
                <span className="nav-dot" aria-hidden="true" />
              )}
            </button>
          ))}
          <p className="nav-caption upcoming-caption">继续生长中</p>
          {upcoming.map((item) => (
            <div
              key={item.label}
              className="nav-item upcoming"
              title={item.description}
            >
              <item.icon size={19} strokeWidth={1.7} aria-hidden="true" />
              <span>{item.label}</span>
              <span className="soon-tag">开发中</span>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-note">
            <LockKeyhole size={15} aria-hidden="true" />
            <span>本机会话 · 仅当前浏览器</span>
          </div>
          <p>
            <CircleHelp size={14} aria-hidden="true" /> 每一次变化，都有迹可循。
          </p>
        </div>
      </aside>
      <div className="workspace">
        <div className="topbar">
          <span>
            我的空间 <span className="crumb">/</span>{" "}
            {navigation.find((item) => item.id === page)?.label}
          </span>
          <span className="version-tag">持续开发中</span>
        </div>
        <main id="main-content" ref={main} tabIndex={-1}>
          {children}
        </main>
        <footer className="workspace-footer">
          LingNexus <span>让经历留下来，让理解慢慢生长。</span>
        </footer>
      </div>
    </div>
  );
}
