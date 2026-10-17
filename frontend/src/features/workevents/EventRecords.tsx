import { useState } from "react";
import { errorMessage } from "../../api/client";
import { coverageLabel, recordKindLabel } from "./api";
import type {
  WorkEventRecord,
  WorkEventRecordPage,
  WorkEventsClient,
} from "./api";

/** 一页正文：分页只追加，游标始终由服务端给出。 */
interface OpenedText {
  sourceId: string;
  location: string;
  coverage: string;
  sha256: string;
  text: string;
  nextCursor: string | null;
}

const pageBytes = 8192;

/**
 * 只读回查：目录分页 + 正文分页。这里只调用 work-events 接口，绝不发聊天或
 * 模型请求；展开内容不会进入任何后续模型输入。
 */
export function EventRecords({
  client,
  eventId,
  coverage,
}: {
  client: WorkEventsClient;
  eventId: string;
  coverage?: string | null;
}) {
  const [page, setPage] = useState<WorkEventRecordPage | null>(null);
  const [loading, setLoading] = useState<"" | "first" | "more">("");
  const [reading, setReading] = useState("");
  const [error, setError] = useState("");
  const [opened, setOpened] = useState<OpenedText | null>(null);

  const load = async (cursor: string | null) => {
    if (loading) return;
    setLoading(cursor ? "more" : "first");
    setError("");
    try {
      const value = await client.records(eventId, cursor, 20);
      setPage((current) =>
        cursor && current
          ? { ...value, items: [...current.items, ...value.items] }
          : value,
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading("");
    }
  };

  const read = async (
    sourceId: string,
    location: string,
    cursor: string | null,
  ) => {
    if (reading) return;
    setReading(sourceId);
    setError("");
    try {
      const value = await client.record(eventId, sourceId, cursor, pageBytes);
      setOpened((current) =>
        cursor && current && current.sourceId === value.source_id
          ? {
              ...current,
              text: current.text + value.text,
              nextCursor: value.next_cursor ?? null,
              coverage: value.coverage,
              location: value.location,
              sha256: value.sha256,
            }
          : {
              sourceId: value.source_id,
              location: value.location || location,
              coverage: value.coverage,
              sha256: value.sha256,
              text: value.text,
              nextCursor: value.next_cursor ?? null,
            },
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setReading("");
    }
  };

  const open = (record: WorkEventRecord) =>
    void read(record.source_id, record.location, null);

  return (
    <div className="work-records">
      <p className="metadata">
        只读查看：目录和正文都直接读本地记录，不发送给模型，也不改变这段工作的
        执行状态。
      </p>
      {!page && (
        <button
          type="button"
          className="button secondary"
          disabled={loading === "first"}
          onClick={() => void load(null)}
        >
          {loading === "first" ? "正在读取…" : "读取记录目录"}
        </button>
      )}
      {error && <p className="notice error">{error}</p>}
      {page && (
        <>
          <p className="metadata">
            已列出 {page.items.length} 条 · 覆盖：
            {coverageLabel(page.coverage ?? coverage)}
          </p>
          {page.items.length === 0 ? (
            <p className="muted small">这段事件还没有可回读的记录。</p>
          ) : (
            <ul className="work-record-list" aria-label="工作记录目录">
              {page.items.map((record) => (
                <li key={record.source_id} className="work-record">
                  <span className="work-record-kind">
                    {recordKindLabel(record.kind)}
                  </span>
                  <span className="work-record-location break-anywhere">
                    {record.location}
                  </span>
                  <span className="metadata">
                    {coverageLabel(record.coverage)}
                    {typeof record.byte_count === "number"
                      ? ` · ${record.byte_count} 字节`
                      : ""}
                  </span>
                  <button
                    type="button"
                    className="text-button"
                    disabled={reading === record.source_id}
                    onClick={() => open(record)}
                  >
                    {reading === record.source_id ? "正在读取…" : "打开正文"}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {page.next_cursor && (
            <button
              type="button"
              className="text-button"
              disabled={loading === "more"}
              onClick={() => void load(page.next_cursor ?? null)}
            >
              {loading === "more" ? "正在读取…" : "读取更多记录"}
            </button>
          )}
        </>
      )}
      {opened && (
        <section className="work-record-text" aria-label="记录正文">
          <div className="work-record-text-head">
            <strong className="break-anywhere">{opened.location}</strong>
            <span className="metadata">
              覆盖：{coverageLabel(opened.coverage)} · 校验：
              {opened.sha256 ? opened.sha256.slice(0, 12) : "未提供"}
            </span>
          </div>
          <pre className="preserve-text work-record-body">{opened.text}</pre>
          <div className="button-row">
            {opened.nextCursor && (
              <button
                type="button"
                className="text-button"
                disabled={!!reading}
                onClick={() =>
                  void read(opened.sourceId, opened.location, opened.nextCursor)
                }
              >
                {reading ? "正在读取…" : "读取下一页"}
              </button>
            )}
            <button
              type="button"
              className="text-button"
              onClick={() => setOpened(null)}
            >
              收起正文
            </button>
          </div>
          <p className="metadata">
            正文按 {pageBytes / 1024} KiB 分页读取，不会一次性载入整段记录。
          </p>
        </section>
      )}
    </div>
  );
}
