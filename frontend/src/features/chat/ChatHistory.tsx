import { Plus } from "lucide-react";
import type { ConversationList } from "./useConversations";

/** Lives in the app sidebar, under the navigation, so switching chats is one click. */
export function ChatHistory({
  conversations,
  currentId,
  disabled,
  onSelect,
  onNew,
}: {
  conversations: ConversationList;
  currentId: string;
  disabled: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <section className="chat-history" aria-label="我的对话">
      <button
        className="chat-history-new"
        disabled={disabled || conversations.creating}
        onClick={onNew}
      >
        <Plus size={16} strokeWidth={2} aria-hidden="true" />
        <span className="chat-history-new-label">新聊天</span>
      </button>
      <p className="nav-caption">最近</p>
      {conversations.loading && (
        <p className="chat-history-note">正在读取对话…</p>
      )}
      {conversations.error && (
        <p className="chat-history-note">
          {conversations.error}
          <button
            className="text-button"
            onClick={() => void conversations.refresh()}
          >
            重新加载
          </button>
        </p>
      )}
      {!conversations.loading &&
        !conversations.error &&
        conversations.list.length === 0 && (
          <p className="chat-history-note">还没有对话，从「新聊天」开始。</p>
        )}
      <ul className="chat-history-list">
        {conversations.list.map((item) => (
          <li key={item.id}>
            <button
              className={
                "chat-history-item" +
                (item.id === currentId ? " active" : "")
              }
              aria-current={item.id === currentId ? "true" : undefined}
              title={item.title}
              disabled={disabled}
              onClick={() => onSelect(item.id)}
            >
              <span className="chat-history-title">{item.title}</span>
              {item.scope.startsWith("scene:") && (
                <span className="chat-history-tag">场景</span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
