import { useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Conversation } from "../../api/types";
import { ErrorNotice } from "../../shared/ui";

/** Shown in the message area while a new conversation is being set up. */
export function NewConversation({
  api,
  agentId,
  disabled,
  onCreated,
  onCancel,
  onCreating,
}: {
  api: LingApi;
  agentId: string;
  disabled: boolean;
  onCreated: (value: Conversation) => void;
  onCancel: () => void;
  onCreating: (busy: boolean) => void;
}) {
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("real");
  const [scene, setScene] = useState("");
  const [background, setBackground] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  const blocked =
    disabled ||
    creating ||
    !title.trim() ||
    (kind === "scene" && (!scene.trim() || background.length > 6000));

  const create = async () => {
    if (blocked) return;
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
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setCreating(false);
      onCreating(false);
    }
  };

  return (
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
      <div className="button-row">
        <button
          type="button"
          className="button secondary"
          disabled={creating}
          onClick={onCancel}
        >
          取消
        </button>
        <button type="submit" className="button primary" disabled={blocked}>
          {creating ? "正在创建…" : "创建对话"}
        </button>
      </div>
    </form>
  );
}
