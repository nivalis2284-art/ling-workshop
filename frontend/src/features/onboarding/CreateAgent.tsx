import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import { ErrorNotice, Mark } from "../../shared/ui";

export function CreateAgent({
  api,
  onCreated,
}: {
  api: LingApi;
  onCreated: (id: string) => Promise<void>;
}) {
  const [name, setName] = useState("灵");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <main className="welcome">
      <div className="welcome-brand">
        <Mark small />
        <strong>LingNexus</strong>
      </div>
      <section className="welcome-card panel">
        <Mark />
        <p className="eyebrow">初次见面</p>
        <h1>从一个名字开始。</h1>
        <p className="muted">
          为你的第一个灵取个名字。此后的对话、资料与任务，都会在这个空间里进行。
        </p>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (busy || !name.trim()) return;
            setBusy(true);
            setError("");
            try {
              const result = await api.createAgent(name.trim());
              await onCreated(result.id);
            } catch (error) {
              setError(errorMessage(error));
              setBusy(false);
            }
          }}
        >
          <label htmlFor="agent-name">灵的名字</label>
          <input
            id="agent-name"
            value={name}
            autoComplete="off"
            maxLength={100}
            required
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
          <p className="field-help">最多 100 个字符，选择一个你喜欢的称呼。</p>
          {error && <ErrorNotice message={error} />}
          <button
            className="button primary wide"
            disabled={busy || !name.trim()}
          >
            {busy ? "正在创建…" : "创建我的灵"}
            <ArrowRight size={17} aria-hidden="true" />
          </button>
        </form>
        <p className="small muted">
          创建后可以添加模型连接，然后开始对话。
        </p>
      </section>
    </main>
  );
}
