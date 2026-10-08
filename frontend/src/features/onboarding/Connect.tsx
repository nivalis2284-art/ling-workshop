import { ArrowUpRight, LockKeyhole } from "lucide-react";
import { Mark } from "../../shared/ui";

export function Connect({ expired = false }: { expired?: boolean }) {
  return (
    <main className="welcome">
      <div className="welcome-brand">
        <Mark small />
        <strong>LingNexus</strong>
      </div>
      <section className="welcome-card panel">
        <Mark />
        <p className="eyebrow">属于你和灵的本地空间</p>
        <h1>连接你的本地空间</h1>
        <p className="muted">
          {expired
            ? "本次会话已结束，需要重新连接。"
            : "还没有找到本次会话的连接信息。"}
        </p>
        <ol className="steps">
          <li>打开电脑上的 LingNexus 启动器，确认服务已启动。</li>
          <li>使用启动器提供的浏览器入口进入此页面。</li>
          <li>连接后，就可以查看你的灵、审阅变化和浏览记忆。</li>
        </ol>
        <div className="notice">
          <LockKeyhole size={19} aria-hidden="true" />
          <p>连接信息由本地入口自动提供，无需填写模型密钥。</p>
        </div>
        <button
          className="button secondary"
          onClick={() => window.location.reload()}
        >
          重新检查连接 <ArrowUpRight size={16} aria-hidden="true" />
        </button>
      </section>
      <p className="welcome-foot">在你的电脑上，留住每一次理解。</p>
    </main>
  );
}
