import { Component } from "react";
import type { ReactNode } from "react";
import { Mark } from "../shared/ui";

export class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed)
      return (
        <main className="welcome">
          <section className="welcome-card panel" role="alert">
            <Mark />
            <h1>页面暂时遇到了问题</h1>
            <p>
              请重新加载页面以读取最新资料。如果刚刚提交了操作，请先核对状态，再决定是否重试。
            </p>
            <button
              className="button primary"
              onClick={() => window.location.reload()}
            >
              重新加载页面
            </button>
          </section>
        </main>
      );
    return this.props.children;
  }
}
