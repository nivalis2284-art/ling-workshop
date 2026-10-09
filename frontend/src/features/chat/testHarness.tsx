import { useMemo, useState } from "react";
import type { LingApi } from "../../api/client";
import { ChatHistory } from "./ChatHistory";
import { ChatPage } from "./ChatPage";
import { ChatSessions } from "./session";
import { useConversations } from "./useConversations";

/**
 * Mirrors how App wires the sidebar list and the chat page together, so tests that
 * exercise the conversation controls do not have to mount the whole shell.
 */
export function ChatHarness({
  api,
  agentId = "ling",
  name = "灵",
}: {
  api: LingApi;
  agentId?: string;
  name?: string;
}) {
  const sessions = useMemo(() => new ChatSessions(api), [api]);
  const session = sessions.forAgent(agentId);
  const conversations = useConversations(
    api,
    agentId,
    session.getSnapshot().conversationId,
  );
  const [newChatOpen, setNewChatOpen] = useState(false);
  const currentId = conversations.selectedId ?? conversations.list[0]?.id ?? "";
  return (
    <>
      <ChatHistory
        conversations={conversations}
        currentId={currentId}
        disabled={conversations.creating}
        onSelect={(id) => {
          conversations.choose(id);
          setNewChatOpen(false);
        }}
        onNew={() => setNewChatOpen(true)}
      />
      <ChatPage
        api={api}
        agent={{ id: agentId, name }}
        session={session}
        conversations={conversations}
        newChatOpen={newChatOpen}
        onCloseNewChat={() => setNewChatOpen(false)}
        openSettings={() => {}}
      />
    </>
  );
}
