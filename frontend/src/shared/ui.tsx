import { AlertCircle, ArrowUpRight, LoaderCircle, Sprout } from "lucide-react";
import type { ReactNode } from "react";

export function Mark({ small = false }: { small?: boolean }) {
  return (
    <span className={"mark" + (small ? " mark-small" : "")} aria-hidden="true">
      <Sprout strokeWidth={1.4} />
    </span>
  );
}

export function PageHeader({
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1 tabIndex={-1}>{title}</h1>
        <p className="muted">{description}</p>
      </div>
      {action}
    </header>
  );
}

export function Loading({ label = "正在读取本地资料…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={20} className="spin" aria-hidden="true" />
      {label}
    </div>
  );
}

export function ErrorNotice({
  message,
  retry,
}: {
  message: string;
  retry?: () => void;
}) {
  return (
    <div className="notice error" role="alert">
      <AlertCircle size={19} aria-hidden="true" />
      <div>
        <p>{message}</p>
        {retry && (
          <button className="text-button" onClick={retry}>
            重新加载 <ArrowUpRight size={14} aria-hidden="true" />
          </button>
        )}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <Sprout size={28} strokeWidth={1.3} aria-hidden="true" />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

export function formatDate(value?: string) {
  if (!value) return "时间未提供";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未提供"
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}
