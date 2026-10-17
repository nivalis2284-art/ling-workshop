import { useState } from "react";
import { Archive, Download, LockKeyhole, LogOut } from "lucide-react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice, PageHeader } from "../../shared/ui";
import { ModelSettings } from "./ModelSettings";
import { PermissionSettings } from "./PermissionSettings";
import { InstructionsSettings } from "./InstructionsSettings";
import { UsagePanel } from "./UsagePanel";
import { SandboxSettings } from "./SandboxSettings";
import { SystemSettings } from "./SystemSettings";
import { BackgroundControls } from "./BackgroundControls";
import { McpSettings } from "./McpSettings";
import { SkillsSettings } from "./SkillsSettings";
import { WorkEventSummarySettings } from "../workevents/WorkEventSettings";

type Operation = "export" | "backup";
const operations = {
  export: {
    title: "导出资料",
    label: "导出文件夹路径",
    action: "导出资料",
    icon: Download,
    hint: "填写一个尚不存在的新文件夹。资料将以便于迁移的格式导出。",
    placeholder: "例如 C:/LingData/export-2026-10-08",
    done: "资料已导出",
  },
  backup: {
    title: "本地备份",
    label: "备份文件路径",
    action: "创建备份",
    icon: Archive,
    hint: "填写一个尚不存在的新文件名，建议以 .db 结尾。",
    placeholder: "例如 C:/LingData/backup-2026-10-08.db",
    done: "备份已完成",
  },
};

export function SettingsPage({
  api,
  agent,
  disconnect,
}: {
  api: LingApi;
  agent: Agent;
  disconnect: () => void;
}) {
  const [paths, setPaths] = useState({ export: "", backup: "" });
  const [confirm, setConfirm] = useState<{
    operation: Operation;
    path: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [leaving, setLeaving] = useState(false);
  function prepare(operation: Operation) {
    const path = paths[operation].trim();
    setError("");
    setSuccess("");
    const normalized = path.replaceAll(String.fromCharCode(92), "/");
    if (!/^(?:[a-zA-Z]:[/]|[/][/][^/]+[/][^/]+)/.test(normalized)) {
      setError("请填写完整的 Windows 路径，例如 C:/LingData/backup.db。");
      return;
    }
    setConfirm({ operation, path });
  }
  async function execute() {
    if (!confirm || busy) return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.maintain(confirm.operation, confirm.path);
      setSuccess(operations[confirm.operation].done + "：" + confirm.path);
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  }
  return (
    <div className="page">
      <PageHeader
        eyebrow="好好保存，共同的经历"
        title="设置"
        description="管理本地资料与当前会话，让重要的记录妥善留存。"
      />
      <ModelSettings api={api} />
      <InstructionsSettings
        key={"instructions-" + agent.id}
        api={api}
        agent={agent}
      />
      <PermissionSettings
        key={"permissions-" + agent.id}
        api={api}
        agent={agent}
      />
      <UsagePanel key={"usage-" + agent.id} api={api} agent={agent} />
      <SandboxSettings api={api} />
      <SystemSettings api={api} />
      <BackgroundControls
        key={"background-" + agent.id}
        api={api}
        agent={agent}
      />
      <WorkEventSummarySettings
        key={"work-events-" + agent.id}
        api={api}
        agent={agent}
      />
      <McpSettings api={api} />
      <SkillsSettings api={api} />
      <section className="panel section-panel">
        <div className="section-heading">
          <h2>数据维护</h2>
          <span className="pill">保存到这台电脑</span>
        </div>
        <p className="muted">
          导出与备份包含当前本地资料库中的所有灵。文件由本地服务写入指定位置。
        </p>
        <div className="maintenance-grid">
          {(Object.keys(operations) as Operation[]).map((operation) => {
            const item = operations[operation];
            return (
              <form
                className="maintenance-card"
                key={operation}
                onSubmit={(event) => {
                  event.preventDefault();
                  prepare(operation);
                }}
              >
                <item.icon size={23} strokeWidth={1.4} aria-hidden="true" />
                <h3>{item.title}</h3>
                <p className="muted small">{item.hint}</p>
                <label htmlFor={operation + "-path"}>{item.label}</label>
                <input
                  id={operation + "-path"}
                  autoComplete="off"
                  value={paths[operation]}
                  placeholder={item.placeholder}
                  required
                  disabled={busy}
                  aria-describedby={operation + "-help"}
                  onChange={(event) =>
                    setPaths({ ...paths, [operation]: event.target.value })
                  }
                />
                <p id={operation + "-help"} className="field-help">
                  目标已存在时服务会拒绝写入，请使用新名称。
                </p>
                <button
                  className="button secondary"
                  disabled={busy || !paths[operation].trim()}
                >
                  {item.action}
                  <item.icon size={15} aria-hidden="true" />
                </button>
              </form>
            );
          })}
        </div>
        {error && <ErrorNotice message={error} />}
        {success && (
          <p className="notice success break-anywhere" role="status">
            {success}
          </p>
        )}
      </section>
      <section className="panel section-panel">
        <div className="section-heading">
          <h2>本机会话</h2>
          <LockKeyhole size={19} aria-hidden="true" />
        </div>
        <p className="muted">
          由启动入口提供当前会话凭据，仅在此浏览器会话中使用。断开后需要从启动入口重新进入。
        </p>
        <button className="button secondary" onClick={() => setLeaving(true)}>
          <LogOut size={15} aria-hidden="true" />
          断开当前会话
        </button>
      </section>
      <section className="development-note">
        <h2>模型与对话</h2>
        <p>保存连接并配置本次运行的密钥后，可以在对话页面选择模型交流。</p>
      </section>
      {confirm && (
        <ConfirmDialog
          title={operations[confirm.operation].action}
          confirmLabel={"确认" + operations[confirm.operation].action}
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void execute()}
        >
          <p>将保存整个本地资料库，包含所有灵的资料。目标位置：</p>
          <p className="path-preview">{confirm.path}</p>
          <p>请确认此位置便于你保管和查找。</p>
        </ConfirmDialog>
      )}
      {leaving && (
        <ConfirmDialog
          title="断开当前会话"
          confirmLabel="确认断开"
          onCancel={() => setLeaving(false)}
          onConfirm={disconnect}
        >
          <p>
            本地资料会保留。再次访问时，请通过 LingNexus 的启动入口重新连接。
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
