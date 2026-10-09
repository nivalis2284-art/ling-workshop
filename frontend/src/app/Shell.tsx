import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  CircleHelp,
  LockKeyhole,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import type { Agent } from "../api/types";
import { Mark } from "../shared/ui";
import { navigation, upcoming } from "./navigation";
import type { PageId } from "./navigation";

const collapseKey = "lingnexus.shell.collapsed";

export function Shell({
  agents,
  agent,
  page,
  onNavigate,
  onSelectAgent,
  conversationNav,
  children,
}: {
  agents: Agent[];
  agent: Agent;
  page: PageId;
  onNavigate: (page: PageId) => void;
  onSelectAgent: (id: string) => void;
  conversationNav?: ReactNode;
  children: ReactNode;
}) {
  const main = useRef<HTMLElement>(null);
  const initial = useRef(true);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(collapseKey) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    if (initial.current) {
      initial.current = false;
      return;
    }
    main.current?.querySelector<HTMLElement>("h1")?.focus();
  }, [page]);
  const toggle = () => {
    setCollapsed((value) => {
      try {
        localStorage.setItem(collapseKey, value ? "0" : "1");
      } catch {
        /* Optional storage. */
      }
      return !value;
    });
  };
  return (
    <div className={"app-shell" + (collapsed ? " collapsed" : "")}>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className={"sidebar" + (collapsed ? " collapsed" : "")}>
        <div className="sidebar-head">
          <div className="brand">
            <Mark small />
            <div>
              <strong>LingNexus</strong>
              <span>与灵，一起生长</span>
            </div>
          </div>
          <button
            type="button"
            className="icon-button sidebar-toggle"
            aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
            aria-expanded={!collapsed}
            title={collapsed ? "展开侧栏" : "收起侧栏"}
            onClick={toggle}
          >
            {collapsed ? (
              <PanelLeftOpen size={16} aria-hidden="true" />
            ) : (
              <PanelLeftClose size={16} aria-hidden="true" />
            )}
          </button>
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
              title={collapsed ? item.label : undefined}
              onClick={() => onNavigate(item.id)}
            >
              <item.icon size={19} strokeWidth={1.7} aria-hidden="true" />
              <span className="nav-label">{item.label}</span>
              {page === item.id && (
                <span className="nav-dot" aria-hidden="true" />
              )}
            </button>
          ))}
          {upcoming.length > 0 && (
            <>
              <p className="nav-caption upcoming-caption">继续生长中</p>
              {upcoming.map((item) => (
                <div
                  key={item.label}
                  className="nav-item upcoming"
                  title={item.description}
                >
                  <item.icon size={19} strokeWidth={1.7} aria-hidden="true" />
                  <span className="nav-label">{item.label}</span>
                  <span className="soon-tag">开发中</span>
                </div>
              ))}
            </>
          )}
        </nav>
        {conversationNav}
        <div className="sidebar-bottom">
          <div className="local-note">
            <LockKeyhole size={15} aria-hidden="true" />
            <span>本机会话 · 仅当前浏览器</span>
          </div>
          <p title="每一次变化，都有迹可循。">
            <CircleHelp size={14} aria-hidden="true" />
            <span>每一次变化，都有迹可循。</span>
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
