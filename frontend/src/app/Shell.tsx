import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { LockKeyhole, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import type { Agent } from "../api/types";
import { Mark } from "../shared/ui";
import { navigation } from "./navigation";
import type { PageId } from "./navigation";

const collapseKey = "lingnexus.shell.collapsed";

/** The shell: a collapsible sidebar and the content surface beside it.
 *
 * Two states only, and they morph into each other (width, fades, sliding
 * labels — never `display` swaps, which made the old collapse twitch):
 * expanded shows the brand row with the collapse control; collapsed shows the
 * rail where the brand mark itself becomes the expand button.
 */
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
    <div
      className={
        "app-shell" +
        (collapsed ? " collapsed" : "") +
        (page === "chat" ? " workspace-chat" : "")
      }
    >
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <div className="sidebar-head">
          <button
            type="button"
            className="brand-mark"
            aria-label={collapsed ? "展开侧栏" : "LingNexus"}
            title={collapsed ? "展开侧栏" : undefined}
            tabIndex={collapsed ? 0 : -1}
            onClick={() => {
              if (collapsed) toggle();
            }}
          >
            <Mark small />
          </button>
          <strong className="brand-name">LingNexus</strong>
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
              <item.icon size={17} strokeWidth={1.7} aria-hidden="true" />
              <span className="nav-label">{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-note">
            <LockKeyhole size={13} aria-hidden="true" />
            <span>本机会话 · 仅当前浏览器</span>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <main id="main-content" ref={main} tabIndex={-1}>
          <div className="page-surface">{children}</div>
        </main>
      </div>
    </div>
  );
}
