export interface Agent {
  id: string;
  name: string;
  created_at?: string;
}
export type SandboxLevel = "local" | "container";
// Frozen shape from GET/PUT /sandbox. protections/limitations are the backend's
// own honest wording and are shown to the owner verbatim.
export interface SandboxStatus {
  level: SandboxLevel;
  available_levels: SandboxLevel[];
  engine: { available: boolean; name: "docker" | "podman" | null };
  protections: string[];
  limitations: string[];
  image: string;
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
export interface AgentInstructions {
  agent_id: string;
  rules: string[];
  defaults: string[];
  customized: boolean;
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
    | "anthropic_messages"
    | "openai_chat_completions"
    | "openai_responses";
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

// Messages hold their own text; the selected branch is the chain up to the head.
export interface ConversationMessage {
  id: string;
  parent_id: string | null;
  role: "user" | "assistant";
  text: string;
  attachment_ids?: string[];
  created_at?: string;
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

// One tool call inside the chat loop. It starts as "running" on tool_started and
// is completed by the matching tool_result (same call_id).
export interface ChatToolCall {
  step: number;
  name: string;
  call_id: string;
  arguments: Record<string, unknown>;
  status?: string;
  preview?: string;
}

// A side-effecting call waiting for the owner's decision (run_command).
export interface PendingConfirmation {
  run_id: string;
  fingerprint: string;
  tool: string;
  arguments?: Record<string, unknown>;
  message?: string;
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
  | {
      event: "notice";
      data: {
        run_id: string;
        code: string;
        message?: string;
        fingerprint?: string;
        tool?: string;
        arguments?: Record<string, unknown>;
      };
    }
  | { event: "step_started"; data: { run_id: string; step: number } }
  | {
      event: "tool_started";
      data: {
        run_id: string;
        step: number;
        name: string;
        call_id: string;
        arguments: Record<string, unknown>;
      };
    }
  | {
      event: "tool_result";
      data: {
        run_id: string;
        step: number;
        name: string;
        call_id: string;
        status: string;
        preview: string;
      };
    }
  | { event: "paused" | "resumed"; data: { run_id: string } }
  | { event: "text" | "thinking"; data: { run_id: string; text: string } }
  | { event: "usage"; data: { run_id: string; usage: ChatUsage } }
  | {
      event: "complete";
      data: {
        run_id: string;
        status: "completed";
        assistant_message_id: string | null;
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
  conversation_id?: string | null;
  user_message_id?: string | null;
  context_manifest?: Record<string, unknown> | null;
  created_at: string;
  finished_at: string | null;
}
