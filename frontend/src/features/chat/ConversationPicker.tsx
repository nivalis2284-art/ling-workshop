import { useState } from "react";
import type { LingApi } from "../../api/client";
import { errorMessage } from "../../api/client";
import type { Conversation, ConversationBranch } from "../../api/types";
import { ErrorNotice } from "../../shared/ui";

export function ConversationPicker({
  api,
  agentId,
  conversations,
  conversationId,
  branches,
  disabled,
  loading,
  onSelect,
  onBranch,
  onCreated,
  onCreating,
}: {
  api: LingApi;
  agentId: string;
  conversations: Conversation[];
  conversationId: string;
  branches: ConversationBranch[];
  disabled: boolean;
  loading: boolean;
  onSelect: (id: string) => void;
  onBranch: (id: string) => void;
  onCreated: (value: Conversation) => void;
  onCreating: (busy: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("real");
  const [scene, setScene] = useState("");
  const [background, setBackground] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const selected = conversations.find((value) => value.id === conversationId);
  const create = async () => {
    if (
      disabled ||
      creating ||
      !title.trim() ||
      (kind === "scene" && (!scene.trim() || background.length > 6000))
    )
      return;
    setCreating(true);
    onCreating(true);
    setError("");
    try {
      const value = await api.createConversation(
        agentId,
        title.trim(),
        kind === "scene" ? scene.trim() : undefined,
        kind === "scene" ? background : undefined,
      );
      onCreated(value);
      setOpen(false);
      setTitle("");
      setScene("");
      setBackground("");
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setCreating(false);
      onCreating(false);
    }
  };
  return (
    <div className="chat-conversations">
      <div className="chat-toolbar">
        <div className="chat-model-field">
          <label htmlFor="chat-conversation">保存的对话</label>
          <select
            id="chat-conversation"
            value={conversationId}
            disabled={disabled || creating || loading || !conversations.length}
            onChange={(event) => onSelect(event.target.value)}
          >
            {!conversations.length && <option value="">从新对话开始</option>}
            {conversations.map((value) => (
              <option value={value.id} key={value.id}>
                {value.title}
                {value.scope.startsWith("scene:") ? " · 场景" : " · 自然"}
              </option>
            ))}
          </select>
        </div>
        <button
          className="button secondary"
          disabled={disabled || creating || loading}
          onClick={() => setOpen(!open)}
        >
          {open ? "收起新建" : "新建对话"}
        </button>
        {!!branches.length && (
          <div className="chat-model-field">
            <label htmlFor="chat-branch">对话分支</label>
            <select
              id="chat-branch"
              value={branches.find((branch) => branch.selected)?.id ?? ""}
              disabled={disabled || creating}
              onChange={(event) => onBranch(event.target.value)}
            >
              {!branches.some((branch) => branch.selected) && (
                <option value="" disabled>
                  选择分支
                </option>
              )}
              {branches.map((branch, index) => (
                <option value={branch.id} key={branch.id}>
                  {branch.selected ? "当前 · " : ""}分支 {index + 1} ·{" "}
                  {branch.text.slice(0, 40)}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
      {selected?.scope.startsWith("scene:") && (
        <p className="metadata chat-scope">场景交流 · {selected.title}</p>
      )}
      {selected?.scope.startsWith("scene:") && selected.background && (
        <details className="chat-scene-background">
          <summary>查看场景背景</summary>
          <p className="preserve-text">{selected.background}</p>
          <p className="metadata">
            这是创建时保存的初始设定，可在后续对话中说明新的设定。
          </p>
        </details>
      )}
      {open && (
        <form
          className="chat-new-conversation"
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <label htmlFor="conversation-title">对话名称</label>
          <input
            id="conversation-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
            disabled={creating || disabled}
          />
          <label htmlFor="conversation-kind">对话类型</label>
          <select
            id="conversation-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            disabled={creating || disabled}
          >
            <option value="real">自然交流</option>
            <option value="scene">场景交流</option>
          </select>
          {kind === "scene" && (
            <>
              <label htmlFor="conversation-scene">场景名称</label>
              <input
                id="conversation-scene"
                value={scene}
                onChange={(event) => setScene(event.target.value)}
                required
                disabled={creating || disabled}
                placeholder="例如：海岛相遇"
              />
              <label htmlFor="conversation-background">场景背景</label>
              <textarea
                id="conversation-background"
                value={background}
                onChange={(event) => setBackground(event.target.value)}
                maxLength={6000}
                rows={4}
                disabled={creating || disabled}
                aria-describedby="scene-help"
                placeholder="写下地点、角色和初始情境…"
              />
              <p className="metadata" id="scene-help">
                背景随场景保存，最多 6000 字。后续对话中可以说明新的设定。
              </p>
            </>
          )}
          {error && <ErrorNotice message={error} />}
          <button
            type="submit"
            className="button primary"
            disabled={
              disabled ||
              creating ||
              !title.trim() ||
              (kind === "scene" && (!scene.trim() || background.length > 6000))
            }
          >
            {creating ? "正在创建…" : "创建对话"}
          </button>
        </form>
      )}
    </div>
  );
}
