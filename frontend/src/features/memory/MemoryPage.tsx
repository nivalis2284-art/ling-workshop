import { useCallback, useState } from "react";
import { ArrowLeft, ArrowRight, BookOpen, Search } from "lucide-react";
import type { LingApi } from "../../api/client";
import type { Agent } from "../../api/types";
import {
  EmptyState,
  ErrorNotice,
  formatDate,
  Loading,
  PageHeader,
} from "../../shared/ui";
import { useResource } from "../../shared/useResource";
import { MemoryDetail } from "./MemoryDetail";
import {
  memoryApi,
  scopeLabels,
  statusLabels,
  type MemoryItem,
  type MemoryKind,
  type MemoryScope,
  type MemoryStatus,
} from "./api";
import "./memory.css";

function MemoryNotebook({ api, agent }: { api: LingApi; agent: Agent }) {
  const [kind, setKind] = useState<MemoryKind>("episodes");
  const [scope, setScope] = useState<MemoryScope>("real");
  const [status, setStatus] = useState<MemoryStatus>("all");
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<MemoryItem | null>(null);
  const [message, setMessage] = useState("");
  const load = useCallback(
    (signal: AbortSignal) =>
      memoryApi(api).items(
        agent.id,
        kind,
        scope,
        status,
        query,
        offset,
        signal,
      ),
    [api, agent.id, kind, scope, status, query, offset],
  );
  const resource = useResource(load);
  function reset() {
    setOffset(0);
    setSelected(null);
    setMessage("");
  }
  return (
    <div className="page memory-notebook">
      <PageHeader
        eyebrow="每一份理解，都有来处"
        title="记忆"
        description="像翻阅笔记一样浏览经历与知识，核对来源，留下你的更正。"
      />
      <div className="memory-toolbar">
        <div role="tablist" aria-label="记忆类型" className="memory-tabs">
          {(["episodes", "knowledge"] as const).map((value) => (
            <button
              key={value}
              role="tab"
              aria-selected={kind === value}
              className={kind === value ? "button primary" : "button secondary"}
              onClick={() => {
                reset();
                setKind(value);
              }}
            >
              {value === "episodes" ? "经历" : "知识"}
            </button>
          ))}
        </div>
        <label>
          记忆范围
          <select
            aria-label="记忆范围"
            value={scope}
            onChange={(event) => {
              reset();
              setScope(event.target.value as MemoryScope);
            }}
          >
            <option value="real">真实经历</option>
            <option value="external">外部资料</option>
          </select>
        </label>
        <label>
          记忆状态
          <select
            aria-label="记忆状态"
            value={status}
            onChange={(event) => {
              reset();
              setStatus(event.target.value as MemoryStatus);
            }}
          >
            <option value="all">全部状态</option>
            <option value="active">使用中</option>
            <option value="archived">已归档</option>
            <option value="invalidated">依据已失效</option>
          </select>
        </label>
      </div>
      {scope === "external" && (
        <p className="notice">
          正在浏览外部资料中的认识；可沿来源核对，不等同于亲历事实。
        </p>
      )}
      <form
        className="search-form"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          reset();
          setQuery(draft.trim());
          if (query === draft.trim() && offset === 0) void resource.refresh();
        }}
      >
        <label className="sr-only" htmlFor="memory-search">
          搜索记忆
        </label>
        <Search size={19} aria-hidden="true" />
        <input
          id="memory-search"
          type="search"
          maxLength={500}
          placeholder="搜索标题、摘要或知识主张…"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button className="button primary" type="submit">
          搜索
        </button>
      </form>
      {message && <p role="status">{message}</p>}
      {selected ? (
        <MemoryDetail
          key={kind + selected.id}
          api={api}
          kind={kind}
          initial={selected}
          back={() => {
            setSelected(null);
            void resource.refresh();
          }}
          deleted={() => {
            setSelected(null);
            setMessage("已删除记忆条目，原始记录仍保留。");
            void resource.refresh();
          }}
        />
      ) : (
        <>
          <div className="section-heading">
            <h2>
              {query
                ? "搜索结果"
                : kind === "episodes"
                  ? "经历片段"
                  : "知识笔记"}
            </h2>
            <span className="metadata">
              {query ? "关键词：" + query : "按记录时间排列 · 包含归档条目"}
            </span>
          </div>
          {resource.status === "loading" && <Loading label="正在读取记忆…" />}
          {resource.status === "error" && (
            <ErrorNotice
              message={resource.error}
              retry={() => void resource.refresh()}
            />
          )}
          {resource.status === "ready" && (
            <>
              {resource.data.length === 0 ? (
                <div className="panel">
                  <EmptyState
                    title={
                      query
                        ? "没有找到相关记忆"
                        : offset
                          ? "已经看完这些记忆了"
                          : "这里还没有记忆条目"
                    }
                  >
                    {query
                      ? "换一个关键词，或清空搜索后再试。"
                      : "可以切换范围或状态，查看其他笔记。"}
                  </EmptyState>
                </div>
              ) : (
                <div className="memory-entries">
                  {resource.data.map((item) => (
                    <article key={item.id} className="panel memory-entry">
                      <div className="memory-entry-icon">
                        <BookOpen
                          size={20}
                          strokeWidth={1.5}
                          aria-hidden="true"
                        />
                      </div>
                      <div className="memory-entry-body">
                        <div className="memory-tags">
                          <span className="pill">
                            {scopeLabels[item.scope]}
                          </span>
                          <span className="pill">
                            {statusLabels[item.status]}
                          </span>
                          {item.provenance === "owner_assertion" && (
                            <span className="pill">主人更正</span>
                          )}
                        </div>
                        <h3>
                          <button
                            onClick={() => {
                              setSelected(item);
                              setMessage("");
                            }}
                          >
                            {item.title ?? item.statement}
                          </button>
                        </h3>
                        {item.summary && (
                          <p className="memory-excerpt">{item.summary}</p>
                        )}
                        <p className="metadata">
                          {item.sources.length} 处来源 · 版本 {item.revision}
                          {item.created_at &&
                            " · " + formatDate(item.created_at)}
                        </p>
                      </div>
                      <ArrowRight size={16} aria-hidden="true" />
                    </article>
                  ))}
                </div>
              )}
              <nav className="pagination" aria-label="记忆分页">
                <button
                  className="button secondary"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 20))}
                >
                  <ArrowLeft size={15} aria-hidden="true" />
                  上一页
                </button>
                <span>第 {Math.floor(offset / 20) + 1} 页</span>
                <button
                  className="button secondary"
                  disabled={resource.data.length < 20}
                  onClick={() => setOffset(offset + 20)}
                >
                  下一页
                  <ArrowRight size={15} aria-hidden="true" />
                </button>
              </nav>
            </>
          )}
        </>
      )}
    </div>
  );
}

export function MemoryPage({ api, agent }: { api: LingApi; agent: Agent }) {
  return <MemoryNotebook key={agent.id} api={api} agent={agent} />;
}
