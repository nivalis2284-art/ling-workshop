import { useMemo } from "react";
import type { LingApi } from "../../api/client";
import { ChatPage } from "./ChatPage";
import { ChatSessions } from "./session";
import { useConversations } from "./useConversations";

/**
 * Mounts the chat page on its own. There is one conversation per individual, so
 * the only thing the page still needs is the conversation list it uses to adopt
 * the backend's default after the first send.
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
  return (
    <ChatPage
      api={api}
      agent={{ id: agentId, name }}
      session={session}
      conversations={conversations}
      openSettings={() => {}}
    />
  );
}
