export interface Agent {
  id: string;
  name: string;
  created_at?: string;
}
export interface AgentPermissions {
  agent_id: string;
  revision: number;
  readable: string[];
  writable: string[];
}
export interface PermissionUpdate {
  readable: string[];
  writable: string[];
  expected_revision: number;
}
export interface AgentUsage {
  scope: "chat" | "all";
  categories?: Record<string, number>;
  calls: number;
  reported_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  provider_total_tokens: number | null;
  cost_usd: string | null;
  cost_status: "unknown" | "estimated" | "partial";
  estimated_cost_usd?: string | null;
  priced_calls?: number;
  note: string;
}
export interface Attachment {
  id: string;
  original_name: string;
  byte_count: number;
  status: "ready" | "partial" | "needs_vision";
  segments: { location: string; text: string }[];
  metadata: Record<string, unknown>;
  warnings: string[];
}
export interface SourceRef {
  id: string;
  revision: number;
}
export interface IdentityState {
  agent_id: string;
  revision: number;
  entries: Record<string, string>;
}
export interface PersonaEntry {
  key: string;
  text: string;
  kind: string;
  origin: { proposal_id: string; kind: string; sources: SourceRef[] } | null;
}
export interface Instructions {
  agent_id: string;
  rules: string[];
  defaults: string[];
  customized: boolean;
}
export interface EntryInput {
  kind: string;
  key: string;
  text: string;
  expected_revision: number;
  request_id: string;
}
export interface EntryWrite {
  state: IdentityState;
  entries: PersonaEntry[];
}
export interface Proposal {
  id: string;
  key: string;
  text: string;
  status: string;
  kind?: string;
  sources: SourceRef[];
  base_revision: number;
  created_at?: string;
}
export interface Revision {
  revision: number;
  entries: Record<string, string>;
  reason?: string;
  created_at?: string;
}
export interface Episode {
  id: string;
  title: string;
  summary: string;
  sources: SourceRef[];
  scope: string;
  created_at?: string;
}
export interface Source extends SourceRef {
  text: string;
  kind?: string;
  scope?: string;
  recorded_at?: string;
}
export interface IdentityBundle {
  state: IdentityState;
  proposals: Proposal[];
  revisions: Revision[];
  entries: PersonaEntry[];
  instructions: Instructions;
}
export interface ModelCapabilities {
  streaming: boolean;
  tools: boolean;
  thinking: boolean;
  effort_levels: string[];
  encrypted_reasoning?: boolean;
  images?: boolean;
  pdf?: boolean;
  temperature?: boolean;
  top_p?: boolean;
  prompt_caching?: boolean;
}
export interface ModelProfile {
  api_family:
    "anthropic_messages" | "openai_chat_completions" | "openai_responses";
  model: string;
  base_url: string;
  capabilities: ModelCapabilities;
  context_window_tokens?: number | null;
  max_output_tokens?: number | null;
  default_effort?: string | null;
  temperature?: number | null;
  top_p?: number | null;
  cache_system_prompt?: boolean;
  // Pydantic serializes Decimal prices as strings; form submissions use numbers.
  input_price_per_million?: number | string | null;
  output_price_per_million?: number | string | null;
  [key: string]: unknown;
}
export interface SavedModel {
  id: string;
  name: string;
  profile: ModelProfile;
  credentials_available: boolean;
}
export interface ModelSelection {
  primary: string | null;
  fallback: string | null;
}
export interface ModelInput {
  name: string;
  profile: ModelProfile;
  profile_id?: string;
  api_key?: string;
}

export interface ChatRequest {
  agent_id: string;
  message: string;
  profile_id?: string;
  request_id: string;
  effort?: string;
  conversation_id?: string;
  regenerate_message_id?: string;
  attachment_ids?: string[];
}

export interface Conversation {
  id: string;
  agent_id: string;
  title: string;
  scope: string;
  head_id: string | null;
  created_at?: string;
  background?: string;
}

export interface ConversationMessage {
  id: string;
  parent_id: string | null;
  role: "user" | "assistant";
  text: string;
  source: SourceRef;
  attachment_ids?: string[];
}

export interface ConversationBranch extends ConversationMessage {
  selected: boolean;
}

export interface ChatUsage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  [key: string]: unknown;
}

export type ChatEvent = { id: string } & (
  | {
      event: "started";
      data: {
        run_id: string;
        conversation_id?: string | null;
        user_message_id?: string | null;
      };
    }
  | {
      event: "model_switch";
      data: {
        run_id: string;
        from_profile: string;
        to_profile: string;
        restart: boolean;
      };
    }
  | { event: "notice"; data: { run_id: string; code: string } }
  | { event: "text" | "thinking"; data: { run_id: string; text: string } }
  | { event: "usage"; data: { run_id: string; usage: ChatUsage } }
  | {
      event: "complete";
      data: {
        run_id: string;
        status: "completed";
        assistant_source: SourceRef | null;
      };
    }
  | { event: "error"; data: { run_id: string; code: string; message: string } }
);

export interface ChatRun {
  id: string;
  agent_id: string;
  request_id: string;
  profile_id: string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
  user_source: SourceRef;
  assistant_source: SourceRef | null;
  context_sources: SourceRef[];
  created_at: string;
  finished_at: string | null;
  conversation_id?: string | null;
  user_message_id?: string | null;
}
