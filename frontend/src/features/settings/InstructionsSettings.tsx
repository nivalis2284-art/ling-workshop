import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, RotateCcw } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent, AgentInstructions } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";
import "./management.css";

// Mirrors the owner instruction limits enforced by the local service.
const MAX_RULES = 50;
const MAX_RULE_LENGTH = 1000;

function cleaned(rules: string[]): string[] {
  return rules.map((rule) => rule.trim()).filter(Boolean);
}

function InstructionEditor({
  api,
  initial,
  onCustomized,
}: {
  api: LingApi;
  initial: AgentInstructions;
  onCustomized: (value: boolean) => void;
}) {
  const [saved, setSaved] = useState(initial);
  const [draft, setDraft] = useState<string[]>(initial.rules);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [confirming, setConfirming] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  const list = cleaned(draft);
  const dirty = JSON.stringify(list) !== JSON.stringify(saved.rules);
  const tooMany = list.length > MAX_RULES;
  const tooLong = list.some((rule) => rule.length > MAX_RULE_LENGTH);

  function edit(index: number, value: string) {
    setDraft(draft.map((rule, position) => (position === index ? value : rule)));
    setSuccess("");
  }
  function remove(index: number) {
    setDraft(draft.filter((_, position) => position !== index));
    setSuccess("");
  }

  async function run(operation: (signal: AbortSignal) => Promise<void>) {
    if (busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await operation(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) setError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (tooMany || tooLong || !dirty) return;
          void run(async (signal) => {
            const result = await api.saveInstructions(
              saved.agent_id,
              cleaned(draft),
              signal,
            );
            if (signal.aborted) return;
            setSaved(result);
            setDraft(result.rules);
            onCustomized(result.customized);
            setSuccess("系统指令已保存，将从下一次对话开始生效。");
          });
        }}
      >
        <p className="metadata">
          共 {list.length} 条 · 上限 {MAX_RULES} 条，每条不超过 {MAX_RULE_LENGTH} 字
        </p>
        {draft.length === 0 ? (
          <p className="small muted">
            当前没有系统指令。新增一条后，它会出现在每次对话的开头。
          </p>
        ) : (
          <ul className="instruction-list">
            {draft.map((rule, index) => (
              <li key={index} className="instruction-item">
                <textarea
                  rows={2}
                  value={rule}
                  disabled={busy}
                  aria-label={"第 " + (index + 1) + " 条系统指令"}
                  placeholder="例如：只报告有依据的经历和真实执行结果"
                  onChange={(event) => edit(index, event.target.value)}
                />
                <button
                  type="button"
                  className="text-button"
                  disabled={busy}
                  aria-label={"删除第 " + (index + 1) + " 条系统指令"}
                  onClick={() => remove(index)}
                >
                  删除
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="button-row">
          <button
            type="button"
            className="button secondary small-button"
            disabled={busy || draft.length >= MAX_RULES}
            onClick={() => {
              setDraft([...draft, ""]);
              setSuccess("");
            }}
          >
            <Plus size={15} aria-hidden="true" />
            新增一条指令
          </button>
        </div>
        <p className="field-help">
          系统指令是所有者写给灵的行为规矩，每次对话开头都会带上。空白条目会被忽略。
        </p>
        {tooMany && (
          <p className="notice error" role="alert">
            系统指令最多 {MAX_RULES} 条。
          </p>
        )}
        {tooLong && (
          <p className="notice error" role="alert">
            单条系统指令不能超过 {MAX_RULE_LENGTH} 字。
          </p>
        )}
        {error && <ErrorNotice message={error} />}
        {success && (
          <p className="notice success" role="status">
            {success}
          </p>
        )}
        <div className="button-row">
          <button
            className="button primary"
            disabled={busy || tooMany || tooLong || !dirty}
          >
            {busy ? "正在保存…" : "保存系统指令"}
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={busy || !saved.customized}
            onClick={() => setConfirming(true)}
          >
            <RotateCcw size={15} aria-hidden="true" />
            恢复程序默认
          </button>
        </div>
        {saved.customized && (
          <details className="instruction-defaults">
            <summary>查看程序默认指令（{saved.defaults.length} 条）</summary>
            <ol>
              {saved.defaults.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ol>
          </details>
        )}
      </form>
      {confirming && (
        <ConfirmDialog
          title="恢复程序默认"
          confirmLabel="确认恢复默认"
          busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() =>
            void run(async (signal) => {
              try {
                const result = await api.resetInstructions(
                  saved.agent_id,
                  signal,
                );
                if (signal.aborted) return;
                setSaved(result);
                setDraft(result.rules);
                onCustomized(result.customized);
                setSuccess("已恢复程序默认指令。");
              } finally {
                if (!signal.aborted) setConfirming(false);
              }
            })
          }
        >
          <p>将丢弃你自定义的系统指令，恢复为程序内置的默认清单。</p>
          <p>此操作只影响系统指令，不会改动对话与资料。</p>
        </ConfirmDialog>
      )}
    </>
  );
}

export function InstructionsSettings({
  api,
  agent,
}: {
  api: LingApi;
  agent: Agent;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.instructions(agent.id, signal),
    [api, agent.id],
  );
  const resource = useResource(load);
  const [customized, setCustomized] = useState<boolean | null>(null);
  const active =
    customized ?? (resource.status === "ready" && resource.data.customized);
  return (
    <section
      className="panel section-panel instruction-settings"
      aria-labelledby="instructions-heading"
    >
      <div className="section-heading">
        <h2 id="instructions-heading">系统指令</h2>
        {resource.status === "ready" && (
          <span className={"pill" + (active ? " warm" : "")}>
            {active ? "已自定义" : "程序默认"}
          </span>
        )}
      </div>
      <p className="muted">
        你写给{agent.name}的行为规矩，每次对话开头都会带上。可以逐条增删改，也可以一键恢复程序默认。
      </p>
      {resource.status === "loading" && <Loading label="正在读取系统指令…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <InstructionEditor
          key={agent.id}
          api={api}
          initial={resource.data}
          onCustomized={setCustomized}
        />
      )}
    </section>
  );
}
