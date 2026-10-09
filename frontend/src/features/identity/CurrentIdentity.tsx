import { useState } from "react";
import type { LingApi } from "../../api/client";
import type { PersonaEntry } from "../../api/types";
import {
  identityEntryLabel,
  identityEntryText,
  kindLabel,
  kindOrder,
} from "../../shared/identityFormatting";
import { Sources } from "../sources/SourceViewer";
import { EntryForm } from "./EntryForm";

function bucketOf(entry: PersonaEntry) {
  return kindOrder.includes(entry.kind) ? entry.kind : "custom";
}

export function CurrentIdentity({
  api,
  entries,
  revision,
  busy,
  add,
  edit,
  remove,
}: {
  api: LingApi;
  entries: PersonaEntry[];
  revision: number;
  busy: boolean;
  add: (kind: string, key: string, text: string) => Promise<void>;
  edit: (kind: string, key: string, text: string) => Promise<void>;
  remove: (key: string) => Promise<void>;
}) {
  const [adding, setAdding] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  return (
    <section className="panel section-panel" aria-labelledby="current-title">
      <div className="section-heading">
        <h2 id="current-title">当前身份</h2>
        <span className="pill">当前版本 · {revision}</span>
      </div>
      <p className="muted small">
        七个部分一直列在这里，空的也显示。点名字展开就能改、能删，也能自己添新的。
      </p>

      <div className="bucket-list">
        {kindOrder.map((kind) => {
          const items = entries.filter((entry) => bucketOf(entry) === kind);
          return (
            <details key={kind} className="bucket">
              <summary>
                <span className="bucket-name">{kindLabel(kind)}</span>
                <span className="bucket-count">{items.length} 条</span>
              </summary>
              <div className="bucket-body">
                {items.length === 0 && adding !== kind && (
                  <p className="muted small">还没有内容。</p>
                )}
                <ul className="persona-entries">
                  {items.map((entry) => (
                    <li key={entry.key} className="entry-row">
                      {editing === entry.key ? (
                        <EntryForm
                          kind={kind}
                          fixedKey={entry.key}
                          initialText={entry.text}
                          busy={busy}
                          onCancel={() => setEditing(null)}
                          submit={(key, text) => edit(kind, key, text)}
                        />
                      ) : (
                        <>
                          <div className="entry-main">
                            <p className="entry-name">
                              {identityEntryLabel(entry.key)}
                            </p>
                            <p className="entry-text">
                              {identityEntryText(entry.key, entry.text)}
                            </p>
                            <details className="entry-origin">
                              <summary>查看这条的依据</summary>
                              {entry.origin ? (
                                <Sources
                                  api={api}
                                  sources={entry.origin.sources}
                                />
                              ) : (
                                <p className="muted small">
                                  这条没有留下可查的依据。
                                </p>
                              )}
                            </details>
                          </div>
                          <div className="row-actions">
                            <button
                              className="button secondary small-button"
                              disabled={busy || editing !== null}
                              aria-label={"修改 " + identityEntryLabel(entry.key)}
                              onClick={() => setEditing(entry.key)}
                            >
                              改
                            </button>
                            <button
                              className="button secondary small-button"
                              disabled={busy || editing !== null}
                              aria-label={"删除 " + identityEntryLabel(entry.key)}
                              onClick={() => void remove(entry.key)}
                            >
                              删
                            </button>
                          </div>
                        </>
                      )}
                    </li>
                  ))}
                </ul>

                {adding === kind ? (
                  <EntryForm
                    kind={kind}
                    busy={busy}
                    onCancel={() => setAdding(null)}
                    submit={(key, text) => add(kind, key, text)}
                  />
                ) : (
                  <div className="button-row">
                    <button
                      className="button secondary small-button"
                      disabled={busy || editing !== null}
                      aria-label={"在" + kindLabel(kind) + "里添加条目"}
                      onClick={() => setAdding(kind)}
                    >
                      + 添加条目
                    </button>
                  </div>
                )}
              </div>
            </details>
          );
        })}
      </div>
    </section>
  );
}
