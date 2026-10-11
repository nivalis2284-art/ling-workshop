import { useCallback, useMemo, useState } from "react";
import { LingApi } from "../api/client";
import { forgetOwnerSession } from "../api/session";
import type { Agent } from "../api/types";
import { useResource } from "../shared/useResource";
import { ErrorNotice, Loading, Mark } from "../shared/ui";
import { Connect } from "../features/onboarding/Connect";
import { CreateAgent } from "../features/onboarding/CreateAgent";
import { OverviewPage } from "../features/overview/OverviewPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { ContentPage } from "../features/content/ContentPage";
import { TasksPage } from "../features/tasks/TasksPage";
import { BrowserPage } from "../features/browser/BrowserPage";
import { BackgroundPage } from "../features/background/BackgroundPage";
import { ChatPage } from "../features/chat/ChatPage";
import { ChatSessions } from "../features/chat/session";
import { useConversations } from "../features/chat/useConversations";
import { Shell } from "./Shell";
import type { PageId } from "./navigation";

/**
 * One workspace per individual. Keyed by agent id so switching individuals resets
 * the conversation selection instead of carrying it across.
 */
function AgentWorkspace({
  api,
  chats,
  agents,
  agent,
  page,
  setPage,
  setSelectedAgent,
  disconnect,
}: {
  api: LingApi;
  chats: ChatSessions;
  agents: Agent[];
  agent: Agent;
  page: PageId;
  setPage: (page: PageId) => void;
  setSelectedAgent: (id: string) => void;
  disconnect: () => void;
}) {
  const chatSession = chats.forAgent(agent.id);
  const conversations = useConversations(
    api,
    agent.id,
    chatSession.getSnapshot().conversationId,
  );
  return (
    <Shell
      agents={agents}
      agent={agent}
      page={page}
      onNavigate={setPage}
      onSelectAgent={setSelectedAgent}
    >
      <div key={agent.id}>
        {page === "overview" && (
          <OverviewPage agent={agent} navigate={setPage} />
        )}
        {page === "chat" && (
          <ChatPage
            api={api}
            agent={agent}
            session={chatSession}
            conversations={conversations}
            openSettings={() => setPage("settings")}
          />
        )}
        {page === "content" && <ContentPage api={api} agent={agent} />}
        {page === "tasks" && (
          <TasksPage
            api={api}
            agent={agent}
            openBrowser={() => setPage("browser")}
          />
        )}
        {page === "browser" && <BrowserPage api={api} agent={agent} />}
        {page === "background" && <BackgroundPage api={api} agent={agent} />}
        {page === "settings" && (
          <SettingsPage api={api} agent={agent} disconnect={disconnect} />
        )}
      </div>
    </Shell>
  );
}

function ConnectedApp({
  api,
  disconnect,
}: {
  api: LingApi;
  disconnect: () => void;
}) {
  const resource = useResource(api.agents);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [page, setPage] = useState<PageId>("overview");
  const chats = useMemo(() => new ChatSessions(api), [api]);
  if (resource.status === "loading")
    return (
      <main className="welcome">
        <Mark />
        <Loading label="正在连接你的本地空间…" />
      </main>
    );
  if (resource.status === "error")
    return (
      <main className="welcome">
        <section className="welcome-card panel">
          <Mark />
          <h1>暂时无法打开空间</h1>
          <ErrorNotice
            message={resource.error}
            retry={() => void resource.refresh()}
          />
          <button className="text-button" onClick={disconnect}>
            返回连接指引
          </button>
        </section>
      </main>
    );
  if (resource.data.length === 0)
    return (
      <CreateAgent
        api={api}
        onCreated={async (id) => {
          setSelectedId(id);
          await resource.refresh();
        }}
      />
    );
  const agent =
    resource.data.find((item) => item.id === selectedId) ?? resource.data[0];
  return (
    <AgentWorkspace
      key={agent.id}
      api={api}
      chats={chats}
      agents={resource.data}
      agent={agent}
      page={page}
      setPage={setPage}
      setSelectedAgent={setSelectedId}
      disconnect={disconnect}
    />
  );
}

export function App({ initialToken }: { initialToken: string | null }) {
  const [token, setToken] = useState(initialToken);
  const [expired, setExpired] = useState(false);
  const disconnect = useCallback(() => {
    forgetOwnerSession();
    setToken(null);
  }, []);
  const onUnauthorized = useCallback(() => {
    setExpired(true);
    forgetOwnerSession();
    setToken(null);
  }, []);
  const api = useMemo(
    () => (token ? new LingApi(token, onUnauthorized) : null),
    [token, onUnauthorized],
  );
  return api ? (
    <ConnectedApp api={api} disconnect={disconnect} />
  ) : (
    <Connect expired={expired} />
  );
}
